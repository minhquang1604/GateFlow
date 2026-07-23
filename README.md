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
- **Lineage** — full chain: Dataset → DatasetVersion → TrainingRun → ModelVersion

## Architecture

```
        Application / Case Study
                  │
                  ▼
        TrainingService             ◄── composes orchestrator + tracker + manager
                  │
   ┌──────────────┼──────────────────────────────┐
   │              │                              │
   ▼              ▼                              ▼
TrainingManager  Orchestrator (ABC)       ExperimentTracker (ABC)
  (lifecycle)        │                              │
                     ├── LocalDockerOrchestrator    ├── MLflowTracker
                     └── AirflowOrchestrator        └── InMemoryTracker
                          (httpx REST)               (lazy mlflow import)
                          │
                          ▼
                    Airflow DAGs
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
│       └── model_version.py
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
├── pipeline/         # (placeholder — registry planned for Week 3)
├── governance/       # (placeholder — policy engine planned for Week 3)
└── lineage/          # (placeholder — LineageManager planned for Week 3)
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
│   ├── pipeline/                # placeholder
│   ├── governance/              # placeholder
│   ├── lineage/                 # placeholder
│   └── exceptions.py
├── tests/
│   ├── unit/                    # 70 unit tests
│   ├── integration/             # 62 integration tests
│   └── _pipelines/              # fixture pipelines for orchestrator tests
├── infrastructure/
│   └── airflow/dags/            # sample DAG for Airflow deployment
├── alembic/
│   ├── versions/                # 001_initial, 002_training_run_lifecycle, 003_models
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
                        └───────────────────┘         │ created_at    │
                                  │                  │ updated_at    │
                                  │ 1:N              └───────┬───────┘
                                  ▼                          │
                        ┌───────────────────┐                │
                        │ model_versions    │                │
                        ├───────────────────┤                │
                        │ id (PK)           │                │
                        │ model_id (FK)     │                │
                        │ dataset_version   │                │
                        │   _id (FK, RESTR) │◀───────────────┘
                        │ training_run_id   │  (SET NULL)
                        │   (FK)            │
                        │ version_number    │
                        │ state (enum)      │
                        │ mlflow_run_id     │
                        │ artifact_uri      │
                        │ metrics_json      │
                        │ notes             │
                        │ created_at        │
                        │ updated_at        │
                        └─────────┬─────────┘
                                  │ N:1
                                  ▼
                        ┌───────────────────┐
                        │      models       │
                        ├───────────────────┤
                        │ id (PK)           │
                        │ name (U)          │
                        │ description       │
                        │ task              │
                        │ created_at        │
                        │ updated_at        │
                        └───────────────────┘
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

## Recommended Next Steps — Week 3

1. **Pipeline registry** — `PipelineRegistry` mapping friendly names
   to orchestrator-executable identifiers per orchestrator.
2. **Governance** — replace placeholder with a policy engine
   (e.g. only one PRODUCTION version per Model; promotion requires
   CANDIDATE → APPROVED).
3. **Lineage** — `LineageManager` that produces lineage graphs from
   the existing FK chain.
4. **Webhooks / events** — replace polling with callbacks.
5. **Real Docker orchestrator** — `DockerOrchestrator` implementing
   the same `Orchestrator` interface.
6. **Production Airflow integration tests** — minimal
   docker-compose with webserver + scheduler to verify the adapter
   against a live REST API.

## License

MIT
