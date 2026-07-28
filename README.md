# MLOps Framework

A reusable MLOps Framework for managing datasets, training runs, model
lifecycle, experiment tracking, and pipeline orchestration — agnostic to
any specific orchestrator or experiment tracker.

## Overview

The framework provides clean abstractions for the full training
lifecycle:

- **Dataset Management** — logical datasets with immutable versioning
- **Dataset Versioning** — checksums, schema hashes, row counts, lineage
- **Training Run Lifecycle** — strict state machine (PENDING → RUNNING → SUCCESS / FAILED / CANCELLED)
- **Orchestration** — pluggable orchestrators (local subprocess, Airflow, …)
- **Experiment Tracking** — pluggable trackers (MLflow, in-memory, …)
- **Model Registry** — `Model` and `ModelVersion` with promotion lifecycle
- **Lineage** — full chain: Dataset → DatasetVersion → TrainingRun → ModelVersion → ServingInstance
- **Dataset Readiness** (Week 3) — explainable READY/BLOCKED decisions, persisted
- **Training Eligibility** (Week 3) — separates "data is ready" from "training should happen"
- **Drift Detection** (Week 3) — pluggable `DriftDetector` ABC, scipy-backed reference
- **Promotion Policy** (Week 3) — explicit, explainable APPROVED/REJECTED
- **Automated Retraining** (Week 3) — framework-controlled end-to-end workflow
- **Model Promotion Events** (Week 3) — `EventPublisher` ABC with HTTP and in-memory adapters
- **Serving Bridge** (Week 3) — FastAPI app that atomically reloads a promoted model

## Architecture

```
        Application / Case Study
                  │
                  ▼
        RetrainingWorkflow         ◄── Week 3: chains all governance
                  │
   ┌──────────────┼──────────────────────────────┐
   │              │              │               │
   ▼              ▼              ▼               ▼
ReadinessEngine  Eligibility  PromotionPolicy  EventPublisher ─▶ ServingBridge
                  Policy            │                │            (FastAPI)
   │              │                ▼                │
   │              │           ModelManager          │
   │              │              │                  │
   │              │              ▼                  │
   │              │      TrainingService            │
   │              │              │                  │
   │              │   ┌──────────┼──────────┐       │
   │              │   ▼          ▼          ▼       │
   │              │ TrainingMgr  Orchestrator  Tracker
   │              │             (ABC)         (ABC)
   │              │               │              │
   │              │   ┌───────────┼────┐    ┌────┴─────┐
   │              │   ▼           ▼    ▼    ▼          ▼
   │              │ LocalDocker  Air  ...  MLflow  InMemory
   │              │              (REST)        (lazy)  (test)
   │              │
   │              ▼
   │          DriftService ─▶ DriftDetector (scipy-backed)
   ▼
LineageManager  ◀── walks the full chain (Dataset → TrainingRun → Model → Serving)
```

### Dependency direction

The framework depends only on its own abstractions (`Orchestrator`,
`ExperimentTracker`). Airflow and MLflow live in adapter modules and
are imported lazily — the framework remains importable (and testable)
without them installed.

### Module layout

```
src/mlops_framework/
├── config/           # Pydantic settings (.env loader)
├── database/         # SQLAlchemy Base, session, ORM models
│   └── models/
│       ├── dataset.py
│       ├── dataset_version.py
│       ├── training_run.py
│       ├── model.py
│       ├── model_version.py
│       ├── readiness_evaluation.py   # Week 3
│       ├── drift_evaluation.py       # Week 3
│       ├── model_promotion_event.py  # Week 3
│       └── serving_instance.py       # Week 3
├── dataset/          # DatasetManager, checksums, schema hashing
├── training/         # TrainingManager, TrainingService, lifecycle state machine
├── orchestration/    # Orchestrator ABC + adapters
│   ├── base.py       # Orchestrator, ExecutionState, ExecutionStatus
│   ├── local.py      # LocalDockerOrchestrator (subprocess)
│   └── airflow.py    # AirflowOrchestrator (httpx REST API)
├── tracking/         # ExperimentTracker ABC + adapters
│   ├── base.py       # ExperimentTracker
│   ├── mlflow.py     # MLflowTracker (lazy import)
│   └── in_memory.py  # InMemoryTracker
├── model/            # ModelManager, lifecycle state machine
├── readiness/        # ReadinessEngine + TrainingPolicy  (Week 3, Day 15)
├── drift/            # DriftDetector ABC + ScipyDriftDetector (Week 3, Day 17)
├── governance/       # TrainingEligibilityPolicy + ModelPromotionPolicy (Day 16/18)
├── workflow/         # RetrainingWorkflow (Week 3, Day 19)
├── events/           # EventPublisher + ModelPromotedEvent (Week 3, Day 20)
├── serving/          # FastAPI ServingBridge (Week 3, Day 20)
└── lineage/          # LineageManager (Week 3, Day 21)
```

## Installation

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in development mode (runtime + dev deps)
pip install -e ".[dev]"

# Optional: install MLflow for the MLflowTracker adapter
pip install mlflow
```

## Database Setup

### Using Docker Compose (PostgreSQL — recommended)

```bash
cp .env.example .env
docker compose up -d
alembic upgrade head
```

The `.env` file is loaded by both `pydantic-settings` and Alembic — no
credentials live in `alembic.ini` or any Python file.

### Using SQLite (testing / local exploration)

Set `DATABASE_URL=sqlite:///./mlops.db` in `.env`. The integration test
suite uses an in-memory SQLite (`:memory:`) via a `StaticPool` — no
setup required.

## Database Migrations

```bash
alembic upgrade head      # apply all migrations
alembic current           # show current revision
alembic history           # show full migration history
alembic revision -m "..." # create a new empty migration
alembic downgrade -1      # roll back one revision
```

Current migration chain:

| Revision | Description |
|----------|-------------|
| `001_initial` | `datasets`, `dataset_versions`, `training_runs` |
| `002_training_run_lifecycle` | Adds `pipeline_id`, `mlflow_run_id`, `error_message` to `training_runs` |
| `003_models` | `models`, `model_versions` tables, `model_state_enum` |
| `004_week3_governance` | `readiness_evaluations`, `drift_evaluations`, `model_promotion_events`, `serving_instances` |

## Usage

All examples use SQLite for brevity; the same code runs against
PostgreSQL by changing `DATABASE_URL`.

### Datasets and versions

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from mlops_framework import DatasetManager

engine = create_engine("sqlite:///./mlops.db")
Session = sessionmaker(bind=engine)
session = Session()

dm = DatasetManager(session)
dataset = dm.create_dataset(name="fraud-detection", description="Credit card fraud data")

version = dm.create_version(
    dataset_id=dataset.id,
    storage_uri="s3://bucket/data/v1.csv",
    row_count=100_000,
    metadata={"columns": [
        {"name": "transaction_id", "dtype": "int64"},
        {"name": "amount",          "dtype": "float64"},
        {"name": "is_fraud",        "dtype": "int64"},
    ]},
)
session.commit()
```

### Training runs — strict lifecycle

The framework owns the lifecycle. Orchestrators and trackers go
through `TrainingManager`; they must not mutate the row directly.

```python
from mlops_framework import TrainingManager
from mlops_framework.exceptions import InvalidStatusTransitionError

tm = TrainingManager(session, dm)
run = tm.create_run(
    dataset_version_id=version.id,
    pipeline_id="fraud-training-pipeline",
    metadata={"model_type": "xgboost"},
)

tm.start_run(run.id)                             # PENDING -> RUNNING
tm.complete_run(run.id)                          # RUNNING  -> SUCCESS

try:
    tm.start_run(run.id)                         # raises — terminal
except InvalidStatusTransitionError as exc:
    print(exc)
```

Allowed transitions:

```
PENDING   -> RUNNING
PENDING   -> CANCELLED
RUNNING   -> SUCCESS
RUNNING   -> FAILED
RUNNING   -> CANCELLED
SUCCESS   | FAILED   | CANCELLED   (terminal)
```

### End-to-end training lifecycle

`TrainingService` composes the orchestrator and the tracker. The same
code works against `LocalDockerOrchestrator` (development) or
`AirflowOrchestrator` (production) — application code does not know
which is plugged in.

```python
from mlops_framework import (
    TrainingManager, TrainingService,
    LocalDockerOrchestrator, InMemoryTracker,
)

orchestrator = LocalDockerOrchestrator()
tracker      = InMemoryTracker()
service      = TrainingService(TrainingManager(session, dm),
                               orchestrator, tracker)

run = service.create_run(
    dataset_version_id=version.id,
    pipeline_id="tests._pipelines.e2e_training:main",
)
service.start_run(run.id)                        # tracker + orchestrator + RUNNING
final_state = service.wait_for_completion(run.id)
# final_state == "SUCCESS"  -> run is now SUCCESS
# final_state == "FAILED"   -> run is now FAILED, error_message captured
```

To swap to Airflow, change one line:

```python
from mlops_framework.orchestration.airflow import AirflowOrchestrator

orchestrator = AirflowOrchestrator(
    base_url="http://airflow.internal:8080",
    username="airflow",
    password="airflow",
)
# ... the rest of the service code is unchanged.
```

### Model registry and lifecycle

```python
from mlops_framework import ModelManager
from mlops_framework.database.models.model_version import ModelState

mm = ModelManager(session)
model = mm.create_model(name="fraud-model", task="fraud_detection")

mv = mm.create_model_version(
    model_id=model.id,
    dataset_version_id=version.id,
    training_run_id=run.id,
    mlflow_run_id=run.mlflow_run_id,
    artifact_uri="s3://models/fraud-v1.pkl",
    metrics={"f1": 0.86, "roc_auc": 0.92},
    state=ModelState.CANDIDATE,
)

mm.transition_state(mv.id, ModelState.APPROVED)
mm.transition_state(mv.id, ModelState.PRODUCTION)
```

Allowed model-version transitions:

```
TRAINING    -> CANDIDATE | REJECTED
CANDIDATE   -> APPROVED  | REJECTED | PRODUCTION
APPROVED    -> PRODUCTION | ARCHIVED | REJECTED
PRODUCTION  -> ARCHIVED
ARCHIVED    | REJECTED   (terminal)
```

### Experiment tracking

Application code calls the framework abstraction, never `mlflow.*`
directly. The same code works with `MLflowTracker` (production) or
`InMemoryTracker` (tests).

```python
from mlops_framework import ExperimentTracker
from mlops_framework.tracking.in_memory import InMemoryTracker
# from mlops_framework.tracking.mlflow import MLflowTracker

tracker: ExperimentTracker = InMemoryTracker()  # or MLflowTracker(...)
tracker.start_run(run_name="exp-42", tags={"pipeline": "fraud"})
tracker.log_params({"n_estimators": 100, "max_depth": 6})
tracker.log_metrics({"f1": 0.86, "roc_auc": 0.92}, step=1)
tracker.log_artifact("model.pkl")
tracker.end_run(status="SUCCESS")
```

### Week 3 — Governance, drift, events, serving

The Week 3 additions layer explicit decision-making on top of the
existing lifecycle. All public types are importable from the top
level.

#### Dataset readiness

```python
from mlops_framework import DatasetManager
from mlops_framework.readiness import ReadinessEngine, TrainingPolicy

dm = DatasetManager(session)
dv = dm.get_version(dataset_version_id)

engine = ReadinessEngine(session)
result = engine.evaluate(
    dv,
    TrainingPolicy(
        required_size=1000,
        freshness_hours=24,
        required_columns=["amount", "is_fraud"],
        dtypes={"amount": "float64", "is_fraud": "int64"},
    ),
)
assert result.is_ready          # or .status == "BLOCKED"
# result.reasons, result.check_dict(), result.to_dict() are all explainable
```

#### Training eligibility

```python
from mlops_framework.governance import (
    TrainingEligibilityPolicy,
    EligibilityConfig,
)

policy = TrainingEligibilityPolicy(session)
decision = policy.evaluate(
    policy.build_context(
        dataset_version=dv,
        readiness=result,        # from the readiness engine
        drift=None,              # or a DriftResult
        model=model,
    ),
    EligibilityConfig(
        require_ready=True,
        cooldown_hours=12,
        min_new_rows=200,
    ),
)
if not decision.eligible:
    print(decision.reasons)     # explainable list
```

#### Drift detection

```python
from mlops_framework.drift import (
    ScipyDriftDetector,
    DriftService,
    DriftConfig,
)

detector = ScipyDriftDetector()    # uses scipy.stats under the hood
service = DriftService(session, detector)
result = service.evaluate(
    reference_version=ref_dv,
    current_version=cur_dv,
    reference_data={"amount": [...], "class": [...]},
    current_data={"amount": [...], "class": [...]},
    config=DriftConfig(threshold=0.05),
)
assert not result.drift_detected
```

#### Promotion policy

```python
from mlops_framework.governance import (
    ModelPromotionPolicy,
    PromotionContext,
    PromotionConfig,
)

decision = ModelPromotionPolicy().evaluate(
    PromotionContext(candidate=mv, production=production_mv),
    PromotionConfig(
        min_metrics={"f1": 0.85, "auprc": 0.80},
        must_beat_production=True,
    ),
)
if decision.approved:
    mm.transition_state(mv.id, ModelState.APPROVED)
    mm.transition_state(mv.id, ModelState.PRODUCTION)
else:
    print(decision.reasons)   # explainable
```

#### Automated retraining workflow

```python
from mlops_framework.workflow import RetrainingWorkflow
from mlops_framework.events import InMemoryEventPublisher

events = InMemoryEventPublisher()
workflow = RetrainingWorkflow(
    session=session,
    training_service=training_service,
    event_publisher=events,
)

outcome = workflow.run(
    dataset_version=dv,
    model=model,
    training_policy=TrainingPolicy(required_size=1000),
    eligibility_config=EligibilityConfig(cooldown_hours=12),
    promotion_config=PromotionConfig(min_metrics={"f1": 0.85}),
    pipeline_id="tests._pipelines.e2e_training:main",
)
if outcome.promoted:
    print(events.events[0].payload)
```

#### Serving bridge

```python
from mlops_framework.serving import ServingBridge
from mlops_framework.database.session import get_db_manager

bridge = ServingBridge(session_factory=get_db_manager().session_factory)

# In a separate process (or test):
import httpx
httpx.post(
    "http://localhost:8000/internal/model/reload",
    json={
        "model_name": "fraud-model",
        "model_version": 3,
        "artifact_uri": "s3://models/fraud-v3.pkl",
    },
)

# Query the active version
httpx.get("http://localhost:8000/internal/model/active/fraud-model")
```

Run the serving bridge locally with `uvicorn`:

```bash
uvicorn my_app:app --factory --app-dir path/to/module
# where my_app:app = ServingBridge(...).app
```

#### End-to-end lineage

```python
from mlops_framework.lineage import LineageManager

graph = LineageManager(session).graph_for_model_version(mv.id)
for node in graph.nodes:
    print(node.type, node.id, node.label)
for edge in graph.edges:
    print(edge.source, "->", edge.target, "(", edge.type, ")")
```

## Configuration

Environment variables (see `.env.example`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | SQLAlchemy database URL | `postgresql+psycopg://postgres:postgres@localhost:5432/mlops_framework` |
| `DATABASE_POOL_SIZE` | Connection pool size | `5` |
| `DATABASE_MAX_OVERFLOW` | Max connections beyond pool | `10` |
| `DATABASE_POOL_TIMEOUT` | Seconds to wait for a pool connection | `30` |
| `DATABASE_ECHO` | Echo SQL to stdout | `false` |
| `APP_NAME` / `APP_VERSION` | Application metadata | `mlops-framework` / `0.1.0` |
| `DEBUG` | Debug mode | `false` |

`alembic.ini` does not contain any credentials. `alembic/env.py` loads
`DATABASE_URL` from `.env` via `python-dotenv` and fails fast if it is
missing.

## Project Structure

```
mlops-framework/
├── src/mlops_framework/         # framework source
│   ├── config/                  # Pydantic settings
│   ├── database/                # SQLAlchemy Base + session + models
│   ├── dataset/                 # DatasetManager, checksums, schema hashing
│   ├── training/                # TrainingManager, TrainingService, lifecycle
│   ├── orchestration/           # Orchestrator ABC + adapters
│   ├── tracking/                # ExperimentTracker ABC + adapters
│   ├── model/                   # ModelManager + lifecycle
│   ├── readiness/               # ReadinessEngine + TrainingPolicy
│   ├── drift/                   # DriftDetector ABC + ScipyDriftDetector
│   ├── governance/              # TrainingEligibilityPolicy + ModelPromotionPolicy
│   ├── workflow/                # RetrainingWorkflow
│   ├── events/                  # EventPublisher ABC + adapters
│   ├── serving/                 # FastAPI ServingBridge
│   ├── lineage/                 # LineageManager
│   └── exceptions.py
├── tests/
│   ├── unit/                    # unit tests
│   ├── integration/             # integration + governance e2e tests
│   └── _pipelines/              # fixture pipelines for orchestrator tests
├── infrastructure/
│   └── airflow/dags/            # sample DAG for Airflow deployment
├── alembic/
│   ├── versions/                # 001_initial, 002, 003, 004_week3_governance
│   ├── env.py
│   └── (no .ini — config in pyproject root)
├── alembic.ini
├── docker-compose.yml           # PostgreSQL
├── pyproject.toml
├── .env.example
└── README.md
```

## Database ERD

```
┌─────────────┐         ┌───────────────────┐         ┌───────────────┐
│  datasets   │         │ dataset_versions  │         │ training_runs │
├─────────────┤         ├───────────────────┤         ├───────────────┤
│ id (PK)     │──1:N──▶│ id (PK)           │──1:N──▶│ id (PK)       │
│ name (U)    │         │ dataset_id (FK)   │         │ dataset_ver   │
│ description │         │ version_number    │         │   _id (FK)    │
│ created_at  │         │ storage_uri       │         │ pipeline_id   │
│ updated_at  │         │ checksum (SHA256) │         │ status        │
└─────────────┘         │ schema_hash       │         │ trigger_type  │
                        │ row_count         │         │ started_at    │
                        │ metadata_json     │         │ completed_at  │
                        │ is_immutable      │         │ mlflow_run_id │
                        │ created_at        │         │ error_message │
                        │ updated_at        │         │ metadata_json │
                        └────┬──────────────┘         │ created_at    │
                             │                        │ updated_at    │
              ┌──────────────┼─────────────┐          └───────┬───────┘
              │              │             │                  │
              ▼ 1:N          ▼ 1:N         ▼ 1:N              │ N:1
   ┌──────────────────┐ ┌──────────────┐ ┌────────────┐      │
   │ readiness_evals  │ │ drift_evals  │ │ model_     │      │
   ├──────────────────┤ ├──────────────┤ │ versions   │◀─────┘ (SET NULL)
   │ id (PK)          │ │ id (PK)      │ ├────────────┤
   │ dataset_version  │ │ reference_   │ │ id (PK)    │
   │   _id (FK)       │ │   dataset_   │ │ model_id   │
   │ status (enum)    │ │   version_id │ │   (FK)     │──▶┌────────────┐
   │ checks_json      │ │ current_     │ │ dataset_   │   │  models    │
   │ reasons_json     │ │   dataset_   │ │   version_ │   ├────────────┤
   │ policy_json      │ │   version_id │ │   id (FK)  │   │ id (PK)    │
   │ snapshot_json    │ │ method       │ │ training_  │   │ name (U)   │
   │ observed_        │ │ outcome      │ │   run_id   │   │ task       │
   │   row_count      │ │ score        │ │ version_   │   │ description│
   │ created_at       │ │ threshold    │ │   number   │   │ created_at │
   │ updated_at       │ │ details_json │ │ state      │   │ updated_at │
   └──────────────────┘ │ created_at   │ │ mlflow_run │   └─────┬──────┘
                        │ updated_at   │ │ artifact_  │         │ 1:N
                        └──────────────┘ │   uri      │         ▼
                                       │ metrics_   │ ┌─────────────────┐
                                       │   json     │ │ serving_        │
                                       │ notes      │ │ instances       │
                                       │ created_at │ ├─────────────────┤
                                       │ updated_at │ │ id (PK)         │
                                       └─────┬──────┘ │ serving_inst_id │
                                             │        │ model_id (FK)   │
                                             │        │ model_version   │
                                             │        │   _id (FK)      │
                                             │        │ is_active       │
                                             │        │ reload_source   │
                                             │        │ created_at      │
                                             │        │ updated_at      │
                                             ▼        └─────────────────┘
                                       ┌──────────────────┐
                                       │ model_promotion_ │
                                       │ events           │
                                       ├──────────────────┤
                                       │ id (PK)          │
                                       │ event_type       │
                                       │ model_id (FK)    │
                                       │ model_version_   │
                                       │   id (FK)        │
                                       │ model_name       │
                                       │ model_version_   │
                                       │   number         │
                                       │ artifact_uri     │
                                       │ metrics_json     │
                                       │ status (enum)    │
                                       │ published_at     │
                                       │ error_message    │
                                       │ created_at       │
                                       │ updated_at       │
                                       └──────────────────┘
```

## Running Tests

```bash
pytest                        # full suite
pytest tests/unit             # unit tests only
pytest tests/integration      # integration tests only
pytest -v                     # verbose
pytest -k orchestrator        # by name
pytest --cov=mlops_framework  # coverage
```

The test suite is hermetic:

- Unit tests for `LocalDockerOrchestrator` spawn real Python
  subprocesses against fixture pipelines in `tests/_pipelines/`.
- Unit tests for `AirflowOrchestrator` use a fake `httpx.Client` — no
  live Airflow needed.
- Integration tests use SQLite in-memory with `StaticPool` so they run
  in any environment.
- End-to-end tests drive the full DatasetVersion → TrainingRun →
  ModelVersion flow through `TrainingService` with the in-memory
  orchestrator and tracker.

## Definition of Done — End of Week 2

- [x] TrainingRun lifecycle with strict state transitions
- [x] Orchestrator abstraction (`Orchestrator` ABC)
- [x] LocalDockerOrchestrator
- [x] AirflowOrchestrator adapter (REST API)
- [x] ExperimentTracker abstraction
- [x] MLflowTracker
- [x] InMemoryTracker (test-friendly)
- [x] `Model` entity
- [x] `ModelVersion` entity
- [x] Model lifecycle state transitions
- [x] DatasetVersion → TrainingRun → ModelVersion lineage
- [x] MLflow Run integration (columns + tracker run ids)
- [x] End-to-end training lifecycle test
- [x] Alembic migrations (3 revisions, head applied)
- [x] 132/132 tests passing (40 Week 1 + 92 Week 2)

## Definition of Done — End of Week 3

- [x] `ReadinessEngine` evaluates a `DatasetVersion` against a
      `TrainingPolicy` and produces a normalized READY/BLOCKED
      decision. Every evaluation is persisted.
- [x] `TrainingEligibilityPolicy` distinguishes dataset readiness
      from training eligibility. Decisions are explainable.
- [x] `DriftDetector` ABC + `ScipyDriftDetector` (KS for numerical,
      chi-square for categorical). External statistical library is
      hidden behind the abstraction.
- [x] `ModelPromotionPolicy` with min-metric thresholds and
      must-beat-production comparison. APPROVED/REJECTED is
      explainable.
- [x] `RetrainingWorkflow` orchestrates
      readiness → drift → eligibility → training → promotion.
      Framework owns the decisions; the orchestrator only executes.
- [x] `EventPublisher` ABC + `InMemoryEventPublisher` and
      `HttpEventPublisher`. `ModelPromotedEvent` is emitted on every
      successful promotion.
- [x] FastAPI `ServingBridge` reloads the model atomically. Prior
      reloads are marked inactive; the active version is queryable.
- [x] `LineageManager` walks the full chain
      `Dataset → DatasetVersion → TrainingRun → ModelVersion →
      ServingInstance`.
- [x] End-to-end governance tests cover all 5 cases (BLOCKED,
      TRAINING FAILURE, MODEL REJECTED, MODEL APPROVED, SERVING
      RELOAD).
- [x] Alembic migration `004_week3_governance` brings the schema
      to head.
- [x] 222/222 tests passing (132 Week 1+2 + 90 Week 3).

## Known Limitations

1. **`LocalDockerOrchestrator` is a subprocess shim, not Docker.**
   The name carries forward compatibility with a future real
   Docker-based implementation. The `Orchestrator` interface is
   identical, so a `DockerOrchestrator` would be a drop-in.
2. **Airflow "cancel" deletes the DAG run.** Airflow 2.x has no clean
   REST endpoint to cancel a running DAG run; deletion is the
   documented workaround.
3. **`pipeline_id` is the orchestrator-executable identifier**
   (e.g. `"tests._pipelines.e2e_training:main"` for local,
   `"mlops_training_pipeline"` for Airflow). A `PipelineRegistry` to
   map friendly names is planned for Week 3.
4. **`TrainingService.wait_for_completion` polls.** Production-grade
   event/callback support is planned for Week 3.
5. **MLflow is optional.** The framework does not require MLflow to
   import or run; `MLflowTracker` is verified to fail with a clear
   framework-level error when mlflow is missing, and `InMemoryTracker`
   is provided as a drop-in for tests.

## Recommended Next Steps — Week 4

1. **Pipeline registry** — `PipelineRegistry` mapping friendly names
   to orchestrator-executable identifiers per orchestrator.
2. **Enforce single PRODUCTION per Model** — currently the
   `RetrainingWorkflow` archives the prior production version on a
   new promotion, but a database-level partial unique index would
   be a stronger guarantee.
3. **Real Docker orchestrator** — `DockerOrchestrator` implementing
   the same `Orchestrator` interface.
4. **Production Airflow integration tests** — minimal
   docker-compose with webserver + scheduler to verify the adapter
   against a live REST API.
5. **Event transport upgrades** — Redis pub/sub or Kafka behind the
   same `EventPublisher` ABC for higher-throughput deployments.
6. **Callback-based completion** — replace `TrainingService` polling
   with the promotion-event bus once a serving process can
   acknowledge the reload.

---

## Week 4 — Management UI, SDK, and Reusability Proof

Week 4 turned the framework into a manageable product without touching
the Week 1-3 internals. Three additions were shipped:

1. **HTTP Management API** — FastAPI façade exposing the existing
   managers as 15 REST endpoints.
2. **Management UI** — server-rendered HTML + vanilla JS (no build
   step) served on the same port as the API.
3. **Python SDK** — `MLOpsProject` so app developers never import a
   manager directly.

### Layered architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    PRESENTATION LAYER                            │
│  Management UI (HTML/CSS/JS, served by FastAPI on :8000)         │
│  - Dashboard  - Datasets  - Runs  - Models  - Lineage           │
└──────────────────────┬───────────────────────────────────────────┘
                       │ HTTP (JSON, fetch API)
┌──────────────────────▼───────────────────────────────────────────┐
│                    FRAMEWORK API (FastAPI)                       │
│  src/mlops_framework/api/                                        │
│  - 15 endpoints for dashboard, datasets, runs, models, lineage,  │
│    readiness                                                      │
│  - Pydantic DTOs, dependency-injected session/manager factories  │
│  - Reuses: DatasetManager, TrainingManager, ModelManager,       │
│            LineageManager, ReadinessEngine                       │
└──────────────────────┬───────────────────────────────────────────┘
                       │ Python calls (no new abstractions)
┌──────────────────────▼───────────────────────────────────────────┐
│                    SDK LAYER (Python facade)                     │
│  src/mlops_framework/sdk/                                        │
│  - MLOpsProject(name) — one entry point                         │
│  - MLOpsDataset, MLOpsRun, MLOpsModel — value objects           │
│  - PipelineRegistry — maps friendly names to orchestrator IDs    │
│  - SDK-level exceptions (MLOpsError, NotFoundError, ...)         │
└──────────────────────┬───────────────────────────────────────────┘
                       │ Python calls
┌──────────────────────▼───────────────────────────────────────────┐
│              EXISTING FRAMEWORK (Week 1-3, unchanged)            │
│  Managers · Services · Workflows · Governance · Events · Serving│
└──────────────────────────────────────────────────────────────────┘
```

**Zero new business logic** was added in Week 4. The API and SDK are
pure façades: they translate HTTP/Python-call inputs into existing
manager method calls and return manager outputs as Pydantic models /
SDK value objects.

### SDK quickstart

```python
from mlops_framework import MLOpsProject

project = MLOpsProject.with_defaults("fraud-detection")
project.register_pipeline(
    "xgboost-training",
    "my_pkg.pipelines:train_xgb",
    description="XGBoost trainer for tabular data",
)

# Datasets
dataset = project.create_dataset("credit-card-transactions")
version = dataset.create_version(
    storage_uri="s3://bucket/transactions-v1.parquet",
    row_count=284_807,
    metadata={"columns": [...]},
)

# Training (returns when done; raises TrainingError on failure)
run = project.train(
    dataset_version=version,
    pipeline="xgboost-training",
    parameters={"max_depth": 6},
    wait=True,
)
print(run.status, run.metrics)

# Models
model = project.get_model("fraud-xgboost")
for mv in model.versions:
    print(mv.version_number, mv.state, mv.metrics)

# Lineage
graph = project.lineage.for_dataset_version(version.id)
# graph["nodes"] / graph["edges"] — ready for any visualisation lib
```

### API quickstart

```bash
# Start the server
uvicorn mlops_framework.api.app:create_app --factory --reload

# Or programmatic:
python -c "from mlops_framework.api.app import create_app; create_app().run()"
```

Then visit:
- `http://localhost:8000/` — Management UI
- `http://localhost:8000/docs` — interactive OpenAPI docs
- `http://localhost:8000/api/dashboard` — JSON KPIs

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/dashboard` | Counts: datasets, versions, runs, production models, success rate |
| GET | `/api/datasets` | List datasets with `version_count` and `latest_version` |
| GET | `/api/datasets/{id}` | Dataset detail |
| GET | `/api/datasets/{id}/versions` | All versions of a dataset |
| GET | `/api/dataset-versions/{id}` | Single version with parsed metadata |
| GET | `/api/training-runs` | List runs (filter by `status`, `dataset_version_id`) |
| GET | `/api/training-runs/{id}` | Run detail with params, metrics, error |
| GET | `/api/models` | List models with production summary |
| GET | `/api/models/{id}` | Model detail |
| GET | `/api/models/{id}/versions` | All versions of a model |
| GET | `/api/model-versions/{id}` | Single version with parsed metrics |
| GET | `/api/lineage/dataset-version/{id}` | Lineage graph JSON |
| GET | `/api/lineage/model-version/{id}` | Lineage graph JSON |
| GET | `/api/lineage/training-run/{id}` | Lineage graph JSON |
| GET | `/api/readiness/{version_id}` | Latest readiness evaluation |

### Reusability proof — two case studies on the same SDK

| Concern | Fraud Detection | Customer Churn |
|---|---|---|
| Data shape | 30 numeric features + binary class | 4 numeric + 2 categorical + binary class |
| Pipeline 1 | `fraud-baseline` | `churn-baseline` |
| Pipeline 2 | `fraud-advanced` | `churn-balanced` |
| Metrics | `f1`, `roc_auc` | `accuracy`, `f1`, `recall` |
| Model name | `fraud-xgboost` | `churn-classifier` |
| Task | binary_classification | binary_classification |
| **SDK import** | `mlops_framework.sdk` | `mlops_framework.sdk` |

Both case studies live at the repository root and are tested with the
exact same SDK. The static check
`TestNoDirectManagerImports::test_app_uses_only_sdk` enforces that the
app code only imports `mlops_framework.sdk` — no managers, no services,
no database models, no orchestrators.

### Test counts

| Layer | Tests | Status |
|---|---|---|
| Week 1-3 baseline (managers, services, governance, workflow) | 222 | unchanged |
| Week 4: PipelineRegistry | 12 | new |
| Week 4: API (6 routers + deps) | 29 | new |
| Week 4: Management UI | 13 | new |
| Week 4: App factory smoke | 2 | new |
| Week 4: SDK | 18 | new |
| Week 4: Fraud Detection case study | 14 | new |
| Week 4: Customer Churn case study | 14 | new |
| **Total** | **324** | passing |

### Running everything

```bash
# Install dev deps
pip install -e ".[dev]"

# Run the test suite
.venv/bin/pytest

# Start the API + UI
.venv/bin/uvicorn mlops_framework.api.app:create_app --factory --reload

# Run the Fraud Detection case study
python -m case_studies.fraud_detection.app

# Run the Customer Churn case study
python -m case_studies.customer_churn.app
```

## Week 5 — Real MLflow + Airflow in Docker Compose

Week 5 ships a runnable local stack: real MLflow, real Airflow, a real
fraud-detection CSV, and a real XGBoost trainer — all coordinated
through the framework's existing `Orchestrator` and `ExperimentTracker`
abstractions. No framework code is changed; the production adapters
(`MLflowTracker`, `AirflowOrchestrator`) just get env-driven defaults
and the demo wires them together.

### What ships

- **7 services in one Compose file** — `postgres`, `minio`, `mlflow`,
  `airflow-common`, `airflow-webserver`, `airflow-scheduler`, `app`,
  `serving`, plus an opt-in `demo` one-shot.
- **A real Airflow DAG** (`infrastructure/airflow/dags/mlops_training_pipeline.py`)
  with three tasks: `resolve_context` → `train` (real XGBoost) →
  `register_and_promote` (PromotionPolicy + serving reload).
- **A real trainer** (`case_studies/fraud_detection/pipelines.py:train_xgboost`)
  that reads the synthetic CSV, fits `xgboost.XGBClassifier`, logs
  metrics + pickle artifact to MLflow, and returns a metrics dict.
- **A one-shot demo entry point** (`scripts/run_end_to_end_demo.py`)
  that walks dataset → version → readiness → eligibility → training
  → promotion → serving reload → lineage, printing each step.

### Quickstart

```bash
# 1. Spin up the stack (Postgres + MinIO + MLflow + Airflow + app + serving)
cp .env.example .env.docker
docker compose --env-file .env.docker up -d

# 2. Apply Alembic migrations once
docker compose --env-file .env.docker run --rm app alembic upgrade head

# 3. Run the demo (one-shot service OR Python out-of-container)
docker compose --env-file .env.docker --profile demo run --rm demo
# or, on the host:
PYTHONPATH=src:. .venv/bin/python scripts/run_end_to_end_demo.py

# 4. Inspect the results
open http://localhost:8000  # Management UI (dashboard, models, lineage)
open http://localhost:5000  # MLflow UI (runs, params, metrics, artifacts)
open http://localhost:8080  # Airflow UI (DAG runs, task logs)
open http://localhost:9001  # MinIO console (artifacts, minioadmin / minioadmin)
```

### Architecture (unchanged from Week 4)

```
┌──────────────────────────────────────────────────────────────────┐
│  PRESENTATION                                                     │
│  Management UI (8000)   MLflow UI (5000)   Airflow UI (8080)     │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION                                                    │
│  AirflowOrchestrator ──httpx──> Airflow REST API (8080)          │
│  LocalDockerOrchestrator ──subprocess──> (Week 1-3 tests)         │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  TRACKING                                                         │
│  MLflowTracker ──mlflow SDK──> MLflow server (5000)              │
│  InMemoryTracker ──dict──> (hermetic tests)                      │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  SERVING                                                          │
│  ServingBridge (8001) ──HTTP──> ModelPromotedEvent publisher      │
└──────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────┐
│  FRAMEWORK CODE (unchanged)                                       │
│  Models · Managers · Services · Policies · Lineage · SDK · API    │
└──────────────────────────────────────────────────────────────────┘
```

The framework does not import Airflow or MLflow in production code.
The Airflow DAG imports the framework (because that's how Airflow
works in real deployments), but the framework's own modules never
import airflow.

### Demo flow in detail

The `scripts/run_end_to_end_demo.py` script performs the following
sequence against the **real** stack:

1. **Wait for health** — `Airflow /health`, `MLflow /health`,
   `ServingBridge /healthz`.
2. **Wire adapters** — `MLflowTracker(experiment="fraud-demo")` and
   `AirflowOrchestrator(...)` are constructed from env-driven
   settings; the `MLOpsProject` SDK is registered with the
   `fraud-xgboost-real` pipeline.
3. **Dataset + Version** — write the synthetic fraud CSV (5 000 rows)
   into `case_studies/fraud_detection/data/transactions.csv` and
   register it via `project.create_dataset(...)`.
4. **Readiness** — `ReadinessEngine.evaluate(...)` with a policy that
   requires 31 columns, 1 000+ rows, and `time/amount/class` dtypes.
5. **Eligibility** — `TrainingEligibilityPolicy.evaluate(...)` with
   `force=True` on the first pass.
6. **Training** — `TrainingService.create_run(...)` registers a
   `TrainingRun`, `MLflowTracker.start_run(...)` opens an MLflow run,
   `AirflowOrchestrator.trigger_pipeline(...)` triggers the
   `mlops_training_pipeline` DAG; the Airflow task trains an
   `XGBClassifier` on the CSV and logs metrics + a pickle artifact.
7. **Wait for DAG completion** — poll `get_execution_status` and
   `get_task_instance_states` until SUCCESS.
8. **Promotion** — `ModelPromotionPolicy.evaluate(...)` with
   `min_metrics={"f1": 0.5}`, then `ModelManager.transition_state` to
   `APPROVED → PRODUCTION` (and archive the prior production).
9. **Serving reload** — `HttpEventPublisher.publish(ModelPromotedEvent)`
   POSTs to the serving bridge `/internal/model/reload`; the bridge
   writes a `ServingInstance` row and reports the active version on
   `/internal/model/active/<name>`.
10. **Lineage** — `LineageManager.graph_for_model_version(...)` prints
    the chain: `Dataset → DatasetVersion → TrainingRun → ModelVersion`.

### Test counts

| Slice | Tests | Status |
|---|---|---|
| Week 1-3 baseline | 324 | unchanged |
| Week 4: API / UI / SDK / case studies | new | passing |
| Week 5: Settings (env overrides) | 8 | new |
| Week 5: MLflowTracker (status mapping, env-fallback, end_run) | 11 | new |
| Week 5: AirflowOrchestrator (task instance states, env-fallback) | 5 | new |
| Week 5: End-to-end demo logic | 10 | new |
| **Total** | **358** | passing |

All 358 tests are hermetic — no live MLflow, Airflow, or Postgres
required. The Docker Compose stack is opt-in for the user.

### Recommended next steps (still open)

- Database-side partial unique index on `models.production_version_id`
  (out of scope for Week 5).
- Distributed event transport (Redis / Kafka) behind the existing
  `EventPublisher` ABC.
- Real container-based Docker orchestrator (replace
  `LocalDockerOrchestrator` with a callable image).
- Callback-based `TrainingService` completion (the demo currently polls).

## License

MIT
