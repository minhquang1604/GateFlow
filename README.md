# MLOps Framework

A reusable MLOps framework for managing datasets, training runs, model
lifecycle, experiment tracking, and pipeline orchestration — agnostic to
any specific orchestrator or experiment tracker. Ships with a management
console called **Gateflow** (dashboard, datasets, runs, models, pipelines,
lineage), a Python SDK, an HTTP API, and two runnable case studies that
prove the abstractions hold up on real, different problems.

📚 **[Full documentation](https://minhquang1604.github.io/GateFlow/)** —
a browsable, searchable version of everything below, organized by topic
rather than one long scroll.

```python
from mlops_framework.sdk import MLOpsProject

project = MLOpsProject.with_defaults("fraud-detection")
project.register_pipeline("xgboost-training", "my_pkg.pipelines:train_xgb")

dataset = project.create_dataset("credit-card-transactions")
version = dataset.create_version(storage_uri="s3://bucket/v1.parquet", row_count=284_807)

run = project.train(dataset_version=version, pipeline="xgboost-training", wait=True)
print(run.status, run.metrics)
```

## Table of contents

- [Quickstart](#quickstart)
- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Installation (local, SQLite)](#installation-local-sqlite)
- [Using the SDK](#using-the-sdk)
- [Using the framework directly](#using-the-framework-directly)
- [Governance: readiness, drift, eligibility, promotion](#governance-readiness-drift-eligibility-promotion)
- [Authentication](#authentication)
- [Gateflow — the management console](#gateflow--the-management-console)
- [Real end-to-end demos](#real-end-to-end-demos)
- [Case studies — reusability proof](#case-studies--reusability-proof)
- [Configuration](#configuration)
- [Database migrations](#database-migrations)
- [Project structure](#project-structure)
- [Database schema](#database-schema)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Feature specifications](#feature-specifications)
- [License](#license)

## Quickstart

The fastest way to see everything work together — real Postgres, real
MinIO, real MLflow, real Airflow, real XGBoost:

```bash
git clone <this-repo> && cd Framework
cp .env.example .env.docker

docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker run --rm app alembic upgrade head

# Full governance chain on a synthetic dataset, driven entirely by the
# framework (dataset → readiness → eligibility → real Airflow DAG →
# promotion → serving reload → lineage):
docker compose --env-file .env.docker --profile demo run --rm demo
```

Then open:

| URL | What you'll see |
|---|---|
| http://localhost:8000 | **Gateflow** — dashboard, the promoted model, its metrics, its lineage graph |
| http://localhost:5000 | MLflow — the real training run: params, metrics, logged artifact |
| http://localhost:8080 | Airflow — the DAG run that actually executed the training (`airflow`/`airflow`) |
| http://localhost:9001 | MinIO console — the uploaded model artifact (`minioadmin`/`minioadmin`) |

No Docker? `pip install -e ".[dev]"` and jump to [Installation](#installation-local-sqlite) for a
SQLite-only path with no external services.

## What it does

- **Dataset Management** — logical datasets with immutable versioning
- **Dataset Versioning** — checksums, schema hashes, row counts, content-hash pinning
- **Training Run Lifecycle** — strict state machine (PENDING → RUNNING → SUCCESS / FAILED / CANCELLED)
- **Orchestration** — pluggable orchestrators (local subprocess, Airflow, …)
- **Experiment Tracking** — pluggable trackers (MLflow, in-memory, …)
- **Model Registry** — `Model` and `ModelVersion` with a promotion lifecycle,
  and a **rollback** path back to a known-good version
- **Lineage** — full chain: DatasetVersion → TrainingRun → ModelVersion → ServingInstance, every version of a dataset shown in parallel (see docs/specs/12-lineage.md)
- **Dataset Readiness** — explainable READY/BLOCKED decisions, persisted
- **Training Eligibility** — separates "data is ready" from "training should happen now"
- **Drift Detection** — pluggable `DriftDetector` ABC, scipy KS-test / chi-square reference implementation
- **Promotion Policy** — explicit, explainable APPROVED/REJECTED, configurable per call
- **Automated Retraining** — one framework-controlled workflow chaining readiness → drift → eligibility → training → promotion
- **Model Promotion Events** — `EventPublisher` ABC with HTTP and in-memory adapters
- **Human Approval** — pluggable `ApprovalGate` ABC (Telegram reference adapter), so an
  automated retrain can block on a real person; denies by default
- **Serving Bridge** — FastAPI app that atomically reloads a promoted model
- **Gateflow** — a server-rendered management console (no build step) over all of the above
- **Python SDK** — `MLOpsProject`, so application code never imports a manager directly

## Architecture

```
        Application / Case Study / SDK
                  │
                  ▼
        RetrainingWorkflow         ◄── chains all governance in one call
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
   │              │      TrainingService             │
   │              │              │                  │
   │              │   ┌──────────┼──────────┐       │
   │              │   ▼          ▼          ▼       │
   │              │ TrainingMgr  Orchestrator  Tracker
   │              │             (ABC)         (ABC)
   │              │               │              │
   │              │   ┌───────────┼────┐    ┌────┴─────┐
   │              │   ▼           ▼    ▼    ▼          ▼
   │              │ LocalDocker  Airflow ...  MLflow  InMemory
   │              │ (subprocess) (REST)        (lazy)  (test)
   │              │
   │              ▼
   │          DriftService ─▶ DriftDetector (scipy-backed)
   ▼
LineageManager  ◀── walks the full chain (DatasetVersion → TrainingRun → ModelVersion → Serving)
                     surfaced by the Gateflow API + console
```

### Dependency direction

The framework depends only on its own abstractions (`Orchestrator`,
`ExperimentTracker`). Airflow and MLflow live in adapter modules and are
imported lazily — the framework stays importable (and testable) without
them installed. The Airflow image itself never imports `mlops_framework`
either (see [Real end-to-end demos](#real-end-to-end-demos)) — Airflow
2.10.4 pins `SQLAlchemy==1.4.x` internally, incompatible with the
framework's own `Mapped`/`mapped_column` models (SQLAlchemy 2.0+), so
the DAG talks to the framework over HTTP
(`mlops_framework.api.routers.internal`) instead.

### Module layout

```
src/mlops_framework/
├── config/           # Pydantic settings (.env loader)
├── database/         # SQLAlchemy Base, session, ORM models
│   └── models/
│       ├── dataset.py / dataset_version.py
│       ├── training_run.py
│       ├── model.py / model_version.py
│       ├── readiness_evaluation.py
│       ├── drift_evaluation.py
│       ├── model_promotion_event.py
│       └── serving_instance.py
├── dataset/          # DatasetManager, checksums, schema hashing
├── training/         # TrainingManager, TrainingService, lifecycle state machine
├── orchestration/    # Orchestrator ABC + adapters
│   ├── base.py       # Orchestrator, ExecutionState, ExecutionStatus
│   ├── local.py      # LocalDockerOrchestrator (a real local subprocess, not Docker)
│   └── airflow.py    # AirflowOrchestrator (httpx REST API)
├── tracking/         # ExperimentTracker ABC + adapters
│   ├── base.py       # ExperimentTracker
│   ├── mlflow.py     # MLflowTracker (lazy import)
│   └── in_memory.py  # InMemoryTracker
├── model/            # ModelManager, lifecycle state machine
├── readiness/        # ReadinessEngine + TrainingPolicy
├── drift/            # DriftDetector ABC + ScipyDriftDetector
├── governance/       # TrainingEligibilityPolicy + ModelPromotionPolicy
├── workflow/         # RetrainingWorkflow — chains all of the above
├── events/           # EventPublisher + ModelPromotedEvent
├── serving/          # FastAPI ServingBridge
├── lineage/          # LineageManager
├── sdk/              # MLOpsProject — the recommended entry point for applications
├── api/              # FastAPI Management API (routers/, schemas.py, app.py)
└── ui/               # Gateflow — server-rendered console (templates/, static/app.js)
```

## Installation (local, SQLite)

For exploring the framework without Docker or Postgres:

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"        # runtime + dev deps (pytest, ruff, httpx)
pip install mlflow              # optional — only needed for MLflowTracker

echo 'DATABASE_URL=sqlite:///./mlops.db' >> .env
alembic upgrade head
```

Start Gateflow + the API:

```bash
uvicorn mlops_framework.api.app:create_app --factory --reload
```

- `http://localhost:8000/` — Gateflow
- `http://localhost:8000/docs` — interactive OpenAPI docs
- `http://localhost:8000/api/dashboard` — JSON KPIs

### Using Postgres instead

```bash
cp .env.example .env
docker compose up -d postgres
alembic upgrade head
```

`.env` is loaded by both `pydantic-settings` and Alembic — no credentials
live in `alembic.ini` or any Python file.

## Using the SDK

`MLOpsProject` is the entry point application code should use — it never
touches a manager, a database model, or an orchestrator directly. Two
[case studies](#case-studies--reusability-proof) in this repo prove the
boundary holds via a static AST test.

```python
from mlops_framework.sdk import MLOpsProject

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

# Reproducibility report — everything needed to cite or reproduce one
# model version: lineage chain, the dataset version's content hash,
# metrics/params (DB + live MLflow), and the full readiness / drift /
# promotion / audit / alert trail. See src/mlops_framework/sdk/report.py.
from pathlib import Path
Path("report.md").write_text(project.report(model_version_id=42))
Path("report.html").write_text(project.report(42, format="html"))
```

`MLOpsProject.with_defaults(name)` wires `LocalDockerOrchestrator` +
`InMemoryTracker` for quick starts; pass `orchestrator=`/`tracker=`
explicitly to point at real Airflow/MLflow (see
[Real end-to-end demos](#real-end-to-end-demos) for a full example).

## Using the framework directly

For framework contributors, or application code that needs governance
primitives the SDK doesn't expose yet.

### Datasets and versions

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from mlops_framework import DatasetManager

engine = create_engine("sqlite:///./mlops.db")
session = sessionmaker(bind=engine)()

dm = DatasetManager(session)
dataset = dm.create_dataset(name="fraud-detection", description="Credit card fraud data")
version = dm.create_version(
    dataset_id=dataset.id,
    storage_uri="s3://bucket/data/v1.csv",
    row_count=100_000,
    metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
)
session.commit()
```

### Training runs — strict lifecycle

Orchestrators and trackers go through `TrainingManager`; nothing mutates
the row directly.

```python
from mlops_framework import TrainingManager
from mlops_framework.exceptions import InvalidStatusTransitionError

tm = TrainingManager(session, dm)
run = tm.create_run(dataset_version_id=version.id, pipeline_id="fraud-training-pipeline")

tm.start_run(run.id)      # PENDING -> RUNNING
tm.complete_run(run.id)   # RUNNING  -> SUCCESS

try:
    tm.start_run(run.id)  # raises — terminal
except InvalidStatusTransitionError as exc:
    print(exc)
```

```
PENDING -> RUNNING -> SUCCESS | FAILED
PENDING -> CANCELLED
RUNNING -> CANCELLED
SUCCESS | FAILED | CANCELLED   (terminal)
```

### End-to-end training — same code, local or Airflow

`TrainingService` composes the orchestrator and the tracker. Swapping
`LocalDockerOrchestrator` for `AirflowOrchestrator` is the only line that
changes.

```python
from mlops_framework import TrainingManager, TrainingService, LocalDockerOrchestrator, InMemoryTracker

service = TrainingService(TrainingManager(session, dm), LocalDockerOrchestrator(), InMemoryTracker())
run = service.create_run(dataset_version_id=version.id, pipeline_id="my_pkg.pipelines:train")
service.start_run(run.id)
final_state = service.wait_for_completion(run.id)   # "SUCCESS" or "FAILED"
```

```python
from mlops_framework.orchestration.airflow import AirflowOrchestrator

orchestrator = AirflowOrchestrator(base_url="http://airflow.internal:8080", username="airflow", password="airflow")
# `pipeline_id` now means the DAG id, not "module:callable" — see
# "AirflowOrchestrator vs LocalDockerOrchestrator" below.
```

#### `AirflowOrchestrator` vs `LocalDockerOrchestrator` — `pipeline_id` means different things

`LocalDockerOrchestrator.trigger_pipeline(pipeline_id, ...)` imports
`pipeline_id` directly as `"module:callable"`. `AirflowOrchestrator.trigger_pipeline(pipeline_id, ...)`
treats `pipeline_id` as the **Airflow `dag_id`** instead — the real
Python callable travels separately, in
`TrainingRun.metadata["training_entrypoint"]`, which the DAG's
`resolve_context`/`train` tasks read back over HTTP (see
`infrastructure/airflow/dags/mlops_training_pipeline.py`). Get this
backwards and `AirflowOrchestrator` 404s trying to trigger a DAG named
after your Python module path. `scripts/run_end_to_end_demo.py` and `demo/steps/retrain.py` both
show the correct pattern.

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
    metrics={"f1": 0.86, "roc_auc": 0.92},
    state=ModelState.CANDIDATE,
)
mm.transition_state(mv.id, ModelState.APPROVED)
mm.transition_state(mv.id, ModelState.PRODUCTION)
```

```
TRAINING   -> CANDIDATE | REJECTED
CANDIDATE  -> APPROVED  | REJECTED | PRODUCTION
APPROVED   -> PRODUCTION | ARCHIVED | REJECTED
PRODUCTION -> ARCHIVED
ARCHIVED | REJECTED   (terminal)
```

### Experiment tracking

Application code calls the framework abstraction, never `mlflow.*`
directly — `InMemoryTracker` in tests, `MLflowTracker` in production.

```python
from mlops_framework.tracking.in_memory import InMemoryTracker

tracker = InMemoryTracker()   # or MLflowTracker(tracking_uri=..., experiment_name=...)
tracker.start_run(run_name="exp-42", tags={"pipeline": "fraud"})
tracker.log_params({"n_estimators": 100, "max_depth": 6})
tracker.log_metrics({"f1": 0.86, "roc_auc": 0.92}, step=1)
tracker.log_artifact("model.pkl")
tracker.end_run(status="SUCCESS")
```

## Governance: readiness, drift, eligibility, promotion

Everything below is what `RetrainingWorkflow` chains automatically — each
piece is also usable standalone.

### Dataset readiness

```python
from mlops_framework.readiness import ReadinessEngine, TrainingPolicy

result = ReadinessEngine(session).evaluate(
    dv,
    TrainingPolicy(required_size=1000, freshness_hours=24,
                    required_columns=["amount", "is_fraud"],
                    dtypes={"amount": "float64", "is_fraud": "int64"}),
)
assert result.is_ready   # or result.status == "BLOCKED"; result.reasons is explainable
```

### Drift detection

```python
from mlops_framework.drift import ScipyDriftDetector, DriftService, DriftConfig

service = DriftService(session, ScipyDriftDetector())   # KS-test (numeric) / chi-square (categorical)
result = service.evaluate(
    reference_version=ref_dv, current_version=cur_dv,
    reference_data={"amount": [...]}, current_data={"amount": [...]},
    config=DriftConfig(threshold=0.05),
)
if result.drift_detected:
    print([f.feature for f in result.feature_results if f.drift_detected])
```

Every evaluation is persisted (`DriftEvaluation`) and browsable per
dataset version in Gateflow, and via `GET /api/drift/{version_id}`.

### Training eligibility

Separates "the data is READY" from "training should happen right now" —
cooldowns, minimum new rows, and (critically) whether drift was actually
observed:

```python
from mlops_framework.governance.eligibility import TrainingEligibilityPolicy, EligibilityConfig

policy = TrainingEligibilityPolicy(session)
decision = policy.evaluate(
    policy.build_context(dataset_version=dv, readiness=result, drift=drift_result, model=model),
    EligibilityConfig(require_drift_to_retrain=True, cooldown_hours=12),
)
if not decision.eligible:
    print(decision.reasons)
```

`require_drift_to_retrain=True` is what turns an automated retraining
workflow into a genuine *reaction* to drift, not a cron job that always
retrains — with no drift, this refuses.

### Promotion policy

```python
from mlops_framework.governance.promotion import ModelPromotionPolicy, PromotionContext, PromotionConfig

decision = ModelPromotionPolicy().evaluate(
    PromotionContext(candidate=mv, production=production_mv),
    PromotionConfig(min_metrics={"f1": 0.85}, must_beat_production=True),
)
if decision.approved:
    mm.transition_state(mv.id, ModelState.APPROVED)
    mm.transition_state(mv.id, ModelState.PRODUCTION)
```

`must_beat_production` compares the candidate against production's
*stored*, training-time metrics on every metric they share. That stops
being a fair bar once the production model's real-world data has
drifted — pass `must_beat_production=False` with an absolute
`min_metrics` floor instead when retraining in reaction to drift (see
[the closed-loop demo](demo/README.md#how-model-v2-is-validated), which
also re-scores the production model on the drifted data to show why the
stored number is not the bar).

### Automated retraining workflow

One call chains readiness → drift (if `drift_service` + reference/current
data are given) → eligibility → training → promotion → event:

```python
from mlops_framework.workflow import RetrainingWorkflow
from mlops_framework.events import InMemoryEventPublisher

workflow = RetrainingWorkflow(session, training_service=service, event_publisher=InMemoryEventPublisher())
outcome = workflow.run(
    dataset_version=dv, model=model,
    training_policy=TrainingPolicy(required_size=1000),
    eligibility_config=EligibilityConfig(cooldown_hours=12),
    promotion_config=PromotionConfig(min_metrics={"f1": 0.85}),
    pipeline_id="my_pkg.pipelines:train",   # LocalDockerOrchestrator convention
)
if outcome.promoted:
    print(outcome.steps)   # every step's pass/fail + explainable detail
```

Works with `AirflowOrchestrator` too — pass the DAG id as `pipeline_id`
and the real callable separately as `training_entrypoint` (see
[pipeline_id means different things](#airfloworchestrator-vs-localdockerorchestrator--pipeline_id-means-different-things)
above):

```python
outcome = workflow.run(
    dataset_version=dv, model=model,
    training_policy=TrainingPolicy(required_size=1000),
    promotion_config=PromotionConfig(min_metrics={"f1": 0.85}),
    pipeline_id="mlops_training_pipeline",   # the Airflow dag_id
    training_entrypoint="my_pkg.pipelines:train",
    training_timeout=600.0,   # a real DAG run needs far longer than the 60s default
)
```

The framework side of this — surfacing the pipeline's metrics back to
the workflow, and not racing the DAG's own callbacks to close out the
run — is handled automatically; see
[Known limitations](#known-limitations) for the one thing that still
needs the DAG itself to cooperate.

### Human approval

An automated retrain that a policy allows is not always one you want run
unattended. `RetrainingWorkflow` takes an optional gate, asked *after*
eligibility and *before* training — no point asking a human about a
retrain the policies already ruled out, and asking before any compute is
spent is the value of the gate.

```python
from mlops_framework.approval.telegram import TelegramApprovalGate

workflow = RetrainingWorkflow(
    session,
    training_service=service,
    approval_gate=TelegramApprovalGate.from_settings(get_settings()),
)
outcome = workflow.run(dataset_version=v, model=m)
# denied -> outcome.blocked_reason == "approval_denied", no TrainingRun created
```

Every gate **denies by default**: a timeout, an unreachable channel, a
malformed reply all return `approved=False`. A gate that could not reach
anyone has not been told yes, and failing open would make it worse than
having none — it would fail open exactly when something is already
wrong. A denial is recorded the way a policy block is (a `RUN_BLOCKED`
event and a `blocked_reason`), because downstream it is the same fact.

`ApprovalGate` is an ABC like `DriftDetector` and `EventPublisher`;
Telegram is a reference adapter, `AutoApproveGate`/`DenyAllGate` make
the two outcomes testable without a channel, and `RecordedDecisionGate`
carries a decision obtained earlier through another channel — for a
caller that must ask before the workflow's own gate point, without
either asking twice or losing the audit row. This used to live inside a
single demo script, wired up by hand.

### Serving bridge

```python
from mlops_framework.serving import ServingBridge

bridge = ServingBridge(session_factory=get_db_manager().session_factory)
# uvicorn my_app:app --factory   where my_app:app = bridge.app
```

```bash
curl -X POST localhost:8001/internal/model/reload \
  -d '{"model_name": "fraud-model", "model_version": 3, "artifact_uri": "s3://models/fraud-v3.pkl"}'
curl localhost:8001/internal/model/active/fraud-model
```

### Starting a training run

```bash
curl -X POST localhost:8000/api/training-runs \
  -H "X-Console-Token: $CONSOLE_WRITE_TOKEN" -H "X-Actor: alice" \
  -d '{"dataset_version_id": 4,
       "training_entrypoint": "case_studies.fraud_detection.pipelines:train_xgboost",
       "model_name": "fraud-xgboost"}'
# 202 {"training_run_id": 17, "status": "RUNNING", "execution_id": "..."}
```

Or **Train now** on a version in the console. Training was previously
startable only from Python (`project.train`) or through
`/api/internal/*`, which is the DAG's own callback surface — reachable,
but not something a console button should be calling.

Create and start are one call on purpose: a created-but-never-started
run is a PENDING row that reads as a stuck training run to everyone
looking at `/runs`, and no caller wants one. The run is committed before
the trigger, because the DAG resolves it by calling
`GET /internal/training-runs/{id}/context` back over HTTP — a separate
transaction, which cannot see an uncommitted row. If Airflow then
refuses the run, it is marked FAILED rather than left PENDING for
nothing to close.

`training_entrypoint` is asked for rather than inferred: `pipeline_id`
means a *dag_id* to `AirflowOrchestrator`, and the `module:callable` the
DAG actually runs is a separate thing that travels in the run's
metadata. Leaving `model_name` unset trains and reports but registers no
ModelVersion — the right default for an exploratory run started by hand.

### Running a drift check

```bash
curl -X POST localhost:8000/api/drift/12/check \
  -H "X-Console-Token: $CONSOLE_WRITE_TOKEN" -d '{}'
# 202 {"dag_id": "mlops_drift_check", "execution_id": "...", ...}
```

Or **Run check** on a version's drift panel in the console. The verdict
appears on `GET /api/drift/{id}` when the DAG finishes.

The indirection is the point. The framework does not read dataset files
— `DriftService` takes feature values from its caller, and nothing under
`src/` opens an S3 object or a CSV — so neither the console nor the API
process can compute drift itself. Giving the app container S3
credentials and a 144 MB CSV inside a 256 MiB reservation is the exact
failure that already killed Airflow's own gunicorn worker once, so the
work goes where the data already is: `mlops_drift_check` reads both
versions, samples them, and posts the values to
`POST /api/internal/drift`.

The DAG does the I/O and nothing else. Which detector, which thresholds
(persisted Settings unless overridden), whether it counts as drift, and
the `DriftEvaluation` row are all decided framework-side — a DAG that
computed its own verdict could assert anything, and the row would be a
client's claim rather than the framework's conclusion. Same split as
`resolve_context`/`readiness` in `mlops_training_pipeline.py`.

Sampling (default 5000 rows/feature) is about transport, not statistics:
a KS test settles on a few thousand points, so shipping 284,807 values
per feature over HTTP would reach the same answer more slowly.

### Rolling back

```python
model = project.get_model("fraud-xgboost")
model.rollback_to(3)          # v3 back into production, incumbent archived
```

Or from the console's Model registry page (**Roll back** on any retired
version), or over HTTP:

```bash
curl -X POST localhost:8000/api/model-versions/12/rollback \
  -H "X-Console-Token: $CONSOLE_WRITE_TOKEN" -H "X-Actor: alice"
```

The promotion policy is deliberately **not** consulted. It answers "is
this candidate good enough to replace production", judged on metrics; a
rollback answers "production is broken, put back the version that
worked", and the version being restored already passed that policy once.
Gating it on metrics would block the rollback in exactly the case it
exists for — an incumbent whose offline metrics look better than the
version you need back. The decision is the operator's; the framework
records it loudly instead (audit row, CRITICAL alert) rather than
second-guessing it.

The HTTP route additionally asks the ServingBridge to reload, and
reports `serving_reloaded` so a caller can tell "the registry rolled
back and serving followed" from "the registry rolled back and serving
may not have". `MLOpsModel.rollback_to` changes the framework's own
registry only — the SDK holds no opinion about where serving lives.

### Lineage

```python
from mlops_framework.lineage import LineageManager

graph = LineageManager(session).graph_for_model_version(mv.id)
for node in graph.nodes:
    print(node.type, node.label, node.attributes)   # TrainingRun nodes carry pipeline_id + mlflow_run_id
for edge in graph.edges:
    print(edge.source, "->", edge.target, f"({edge.type})")
```

## Authentication

Two credentials are accepted, and the difference between them is the
point.

A **scoped API key** resolves to a named principal, and that name is what
lands in `AuditLog.actor` — derived from a credential the caller had to
possess, so an audit row is evidence rather than a claim.

```bash
# The first key cannot be minted through the API (that needs `admin`),
# so it is created against the database directly — same trust boundary
# as running a migration.
python -m mlops_framework.auth.cli create alice --scopes admin
#     mlops_ak_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
#     This is the only time it will be shown.

curl -X POST localhost:8000/api/api-keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"name": "airflow-dag", "scopes": ["write"]}'

curl -X POST localhost:8000/api/model-versions/12/rollback \
  -H "Authorization: Bearer $ALICE_KEY"      # actor = "alice", verified
```

| Scope | Grants |
|---|---|
| `read` | Every GET the console renders from |
| `write` | Anything that changes state — promote, rollback, start a run, schedules, policies. Implies `read` |
| `admin` | Managing keys. Implies `write` |

Only a **hash** of a key is stored; the plaintext is returned once and is
unrecoverable, so a database dump yields no usable credential. Revocation
sets a timestamp rather than deleting the row — a key that acted has to
stay resolvable for as long as the audit rows naming it do.

The **shared secret** (`CONSOLE_WRITE_TOKEN`, `X-Console-Token`) still
works and grants `write`. It is what closed the anonymous-write hole and
what every current deployment is configured with; a request using it
records the unverified `X-Actor` header as its actor, exactly as before.
It deliberately does **not** grant `admin`: a shared secret that could
mint per-principal keys would let anyone holding it manufacture
identities.

Refusals distinguish the two failures: **401** when nothing valid was
presented (an unknown key and a wrong key are the same answer, so
neither confirms that a string is real), **403** when the caller is known
but lacks the scope, and **503** when the deployment has no keys and no
shared secret — it cannot authenticate anyone, and saying so beats a 401
no credential could satisfy.

## Gateflow — the management console

A server-rendered console (HTML + vanilla JS, no build step) served on
the same FastAPI app as the API, at `/`.

| Page | Route | Shows |
|---|---|---|
| Dashboard | `/dashboard` | Dataset/run/model counts, success rate |
| Datasets | `/datasets/{id}` | Versions, schema, readiness panel, **drift panel** with a **Run check** button |
| Datasets (version panel) | `/datasets/{id}` | **Train now** on any version — creates the run and hands it to Airflow |
| Training runs | `/runs/{id}` | Params, metrics, error, MLflow panel, Airflow task grid (per-task Clear/Retry, gated — see [Configuration](#configuration)'s `CONSOLE_WRITE_TOKEN`) |
| Models | `/models/{id}` | Versions, metrics, production state, per-version **reproducibility report** download, and **Roll back** on any retired version |
| Pipelines | `/pipelines/{dag_id}` | Airflow DAG Graph View + task-instance history grid |
| Lineage | `/lineage` | Full DatasetVersion → … → ServingInstance graph, every dataset version in parallel, click-through |
| Settings | `/settings` | Effective MLflow/Airflow/database config, secrets masked, live reachability ping |
| Activity | `/activity` | Two tabs: **Audit trail** (who/what triggered a schedule or promotion decision — see `audit/manager.py`) and **Alerts** (what the framework itself detected — training failures, drift, blocked retrains — see `events/store.py`) |

### API reference

64 REST endpoints under `/api`, plus the two probes at the root
(`/health`, `/ready`), grouped by what they front:

| Group | Examples | Purpose |
|---|---|---|
| Framework rows | `/api/dashboard`, `/api/datasets`, `/api/training-runs/{id}`, `/api/models/{id}`, `/api/readiness/{version_id}`, `/api/drift/{version_id}` | Thin façades over the managers — zero new business logic. List endpoints take `limit`/`offset` (default 200, max 1000) and return the unpaged total in `X-Total-Count` |
| Lineage | `/api/lineage/{dataset\|dataset-version\|model-version\|training-run}/{id}` | Lineage graph JSON |
| Airflow proxy | `/api/airflow/health`, `/api/airflow/dags/{id}`, `/api/training-runs/{id}/tasks` | Live DAG/task state for the pipeline detail page |
| Airflow task control | `/api/training-runs/{id}/tasks/{task_id}/clear`, `.../retry` | Gated write endpoints (`api/security.py::require_write_token`) — fix a stuck task without leaving Gateflow |
| MLflow proxy | `/api/training-runs/{id}/mlflow`, `/api/mlflow/experiments`, `/api/mlflow/registered-models` | Live run/experiment data for the run detail page |
| Settings | `/api/settings` | Effective config + live reachability for the database, MLflow, Airflow |
| Audit trail | `/api/audit` | Who/what triggered a schedule or promotion decision — `audit/manager.py` |
| Alerts | `/api/alerts` | What the framework itself detected (training failures, drift, blocked retrains) — `events/store.py` |
| Start training | `POST /api/training-runs` | Create a run and hand it to Airflow in one gated, audited call. 202; progress on `GET /api/training-runs/{id}` and the SSE stream |
| Drift | `/api/drift/{id}` (read), `/api/drift/{id}/check` (run) | The check is queued on Airflow, not run in-process — see below. 202 on trigger; the verdict lands on the read endpoint |
| API keys | `/api/api-keys` | Mint (returns the key once), list, revoke. Requires the `admin` scope |
| Rollback | `/api/model-versions/{id}/rollback` | Put a retired version back into production — archives the incumbent, audits the actor, raises a CRITICAL alert, and asks the ServingBridge to reload. Gated by `CONSOLE_WRITE_TOKEN` |
| Report | `/api/model-versions/{id}/report` | Download a self-contained reproducibility report (`?format=markdown\|html`) — `sdk/report.py` |
| Health | `/health`, `/ready` | Liveness (process only) and readiness (pings the database) — mounted at the root, not under `/api`, for container/load-balancer probes |
| Internal | `/api/internal/*` | The Airflow DAG's own callbacks (`resolve_context`, `finish`, `promote`) — the only route into the database from outside the docker network. Gated by `CONSOLE_WRITE_TOKEN`, whole router, GET included |

Full list at `/docs` (OpenAPI) once the app is running.

## Real end-to-end demos

Three entry points, each proving a different slice against the **real**
Docker Compose stack — no mocks. All assume
`docker compose --env-file .env.docker up -d` has been run (see
[Quickstart](#quickstart)) and, when run from the host rather than
inside a container, that MLflow/MinIO/Airflow env vars are set (already
present in `.env.example` — `cp .env.example .env` covers a host run).

| Script | Proves | Orchestrator |
|---|---|---|
| `scripts/run_end_to_end_demo.py` | The full governance chain — dataset → readiness → eligibility → training → promotion → serving reload → lineage — on a synthetic dataset | `AirflowOrchestrator` (real DAG) |
| `scripts/run_fraud_detection_e2e.py` | The same chain on the **real** 284,807-row Kaggle credit-card-fraud dataset; gates promotion on `average_precision` (the metric that actually matters at a 0.17% positive rate) | `LocalDockerOrchestrator` (training) + `AirflowOrchestrator` (adapter proof only) |
| `demo/run_closed_loop_demo.py` | **The closed loop**, end to end: Dataset V1 → Model V1 → production monitoring (a baseline window that must *not* flag) → a controlled covariate shift → real KS-test detection → Telegram approval → Dataset V2 = V1 + the drifted data → retrain → validate → archive V1 → promote V2. Has its own [README](demo/README.md). | `AirflowOrchestrator` (real DAG) for **both** training runs — the generated datasets live on a bind mount both sides can read |

```bash
# 1. Full governance chain (synthetic data, ~5s)
docker compose --env-file .env.docker --profile demo run --rm demo

# 2. Real Kaggle dataset (needs case_studies/fraud_detection/data/creditcard.csv —
#    see case_studies/fraud_detection/README.md to download it)
MLFLOW_TRACKING_URI=http://localhost:5000 AIRFLOW_BASE_URL=http://localhost:8080 \
  AIRFLOW_USERNAME=airflow AIRFLOW_PASSWORD=airflow \
  PYTHONPATH=src:. .venv/bin/python -m scripts.run_fraud_detection_e2e

# 3. The closed loop — drift, human approval, retrain, promote.
#    No setup step: the datasets it generates (including V2, which is
#    built during the run) live on the demo/data bind mount, which the
#    Airflow containers see at /opt/demo_data.
docker compose --env-file .env.docker --profile demo run --rm \
  -e DEMO_ARGS="--mode interactive" demo
```

Every script's own module docstring documents its exact flow — read it
before running if you're adapting one.

## Case studies — reusability proof

Two self-contained apps at the repo root, consuming the framework
through the SDK only — see `case_studies/README.md`.

| | Fraud Detection | Customer Churn |
|---|---|---|
| Domain | Credit-card fraud | 30-day telecom churn |
| Data shape | 30 numeric features | 4 numeric + 2 categorical |
| Metrics | `f1`, `average_precision` | `accuracy`, `f1`, `recall` |
| SDK import | `mlops_framework.sdk` | `mlops_framework.sdk` |

```bash
python -m case_studies.fraud_detection.app
python -m case_studies.customer_churn.app
```

`case_studies/*/tests/test_use_case.py::TestNoDirectManagerImports` is a
static AST check that fails CI if either case study ever imports a
manager, service, database model, or orchestrator directly — the SDK
boundary is enforced, not just documented.

## Configuration

Environment variables (`.env` for host runs / Alembic, `.env.docker` for
`docker compose`) — see `.env.example`:

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy database URL | `postgresql+psycopg://postgres:postgres@localhost:5432/mlops_framework` |
| `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` / `DATABASE_POOL_TIMEOUT` | Connection pool tuning | `5` / `10` / `30` |
| `DATABASE_ECHO` | Echo SQL to stdout | `false` |
| `MLFLOW_TRACKING_URI` | MLflow tracking server URL used by `MLflowTracker` | unset (falls back to `http://localhost:5000` in scripts) |
| `MLFLOW_EXPERIMENT_NAME` | Default MLflow experiment | `mlops-framework` |
| `MLFLOW_S3_ENDPOINT_URL` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | MinIO/S3 credentials the MLflow **client** needs directly — it talks to the artifact store, bypassing the mlflow server. Missing these fails `log_artifact`/`download_artifacts` with a silent `AccessDenied`, not a crash | unset |
| `AIRFLOW_BASE_URL` / `AIRFLOW_USERNAME` / `AIRFLOW_PASSWORD` | `AirflowOrchestrator` REST credentials | unset / `airflow` / `airflow` |
| `SERVING_BRIDGE_URL` | Used by `HttpEventPublisher` | unset |
| `CONSOLE_WRITE_TOKEN` | Shared secret required in the `X-Console-Token` header by **every** state-changing endpoint: all of `/api/internal/*` (the Airflow DAG's callbacks) and the write half of `/api/schedules`, plus Airflow task Clear/Retry — see `api/security.py`. The gate fails closed: unset, those endpoints answer 503, so the DAG cannot report a run back and Scheduling is read-only. Reads are never gated | unset |
| `APP_NAME` / `APP_VERSION` | Application metadata | `mlops-framework` / `0.1.0` |
| `DEBUG` | Debug mode | `false` |

`alembic.ini` holds no credentials — `alembic/env.py` loads
`DATABASE_URL` from `.env` via `python-dotenv` and fails fast if it's
missing.

Inside `docker compose`, `app`/`serving`/`demo`/`airflow-webserver`/
`airflow-scheduler` already have the MinIO credentials and in-network
service URLs (`http://mlflow:5000`, `http://airflow-webserver:8080`, …)
set in `docker-compose.yml` — you only need to export them yourself when
running a script on the host.

## Database migrations

```bash
alembic upgrade head      # apply all migrations
alembic current            # show current revision
alembic history             # show full migration history
alembic revision -m "..."  # create a new empty migration
alembic downgrade -1        # roll back one revision
```

| Revision | Adds |
|---|---|
| `001_initial` | `datasets`, `dataset_versions`, `training_runs` |
| `002_training_run_lifecycle` | `pipeline_id`, `mlflow_run_id`, `error_message` on `training_runs` |
| `003_models` | `models`, `model_versions`, `model_state_enum` |
| `004_week3_governance` | `readiness_evaluations`, `drift_evaluations`, `model_promotion_events`, `serving_instances` |

## Project structure

```
Framework/
├── src/mlops_framework/     # framework source — see Module layout above
├── case_studies/            # fraud_detection/, customer_churn/ — SDK consumers
├── demo/                    # closed-loop demo — runner, steps/, its own README
│   ├── run_closed_loop_demo.py
│   ├── config.py             # every seed/threshold, in one place
│   ├── steps/                # one module per lifecycle phase
│   └── data/                 # generated datasets (gitignored, bind-mounted)
├── scripts/                 # other end-to-end demo entry points
├── tests/
│   ├── unit/                 # unit tests
│   ├── integration/          # integration + governance e2e tests
│   ├── api/                  # FastAPI TestClient tests
│   ├── demo/                 # closed-loop lifecycle + safety-invariant tests
│   └── _pipelines/           # fixture pipelines for orchestrator tests
├── infrastructure/
│   ├── airflow/               # Dockerfile, entrypoint.sh, dags/
│   ├── app/                   # Dockerfile for app/serving/demo
│   ├── mlflow/, minio/, postgres/
│   └── terraform/             # AWS deployment (see docs/aws-deployment-plan.md)
├── alembic/                  # versions/, env.py
├── docker-compose.yml
├── pyproject.toml
├── .env.example
└── README.md
```

## Database schema

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
                        │ parent_version_id │         │               │
                        │   (FK, self)      │         │               │
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

## Testing

```bash
pytest                         # full suite — 973 passed, 24 skipped (live-service integration tests)
pytest tests/unit              # unit tests only
pytest tests/integration       # integration tests only
pytest -k drift                # by name
pytest --cov=mlops_framework   # coverage — currently 91%
```

Every test carries a 300s ceiling (`pytest-timeout`, configured in
`pyproject.toml`). Several drive real subprocesses and a real local
MLflow store, so a hang there used to stall the whole run with nothing
naming the test responsible.

The suite is hermetic — no live MLflow, Airflow, or Postgres required:

- `LocalDockerOrchestrator` unit tests spawn real Python subprocesses
  against fixture pipelines in `tests/_pipelines/`.
- `AirflowOrchestrator` unit tests use a fake `httpx.Client`.
- Integration tests use in-memory SQLite (`StaticPool`).
- `tests/api/` boots the real FastAPI app via `TestClient` against an
  in-memory database.

The 24 skipped tests need a live stack (`docker compose up -d`) and are
opt-in — see `tests/integration/test_airflow_live.py`.

CI runs `ruff check .` over the whole repository, not just `src/`, and
`pytest tests/ case_studies/` — `pytest tests/` alone was skipping the
two case studies that exist to prove the SDK boundary holds. Deliberate
lint exceptions live in `pyproject.toml`'s
`[tool.ruff.lint.per-file-ignores]`, each with the reason written next
to it.

## Known limitations

1. **`LocalDockerOrchestrator` is a subprocess shim, not Docker.** The
   name carries forward compatibility with a future real Docker-based
   implementation; the `Orchestrator` interface is identical, so a
   `DockerOrchestrator` would be a drop-in.
2. **`RetrainingWorkflow` + `AirflowOrchestrator` needs the DAG's
   cooperation, not just `training_entrypoint`.** The framework side is
   handled — metrics reported out-of-band survive
   `wait_for_completion()`'s own status merge (see
   `TrainingService.wait_for_completion`), and `RetrainingWorkflow`
   itself never races anyone to close out the run. But
   `mlops_training_pipeline.py`'s `register_and_promote` and
   `report_status` tasks own registration/promotion by default (that is
   the right behavior for the demo scripts, which call the DAG
   directly, not through `RetrainingWorkflow`) — they only step back
   when the run's metadata carries `owned_by_workflow` (set
   automatically by `RetrainingWorkflow.run()`). A custom DAG that
   doesn't check that flag would double-register a ModelVersion and
   evaluate it against two different promotion policies. See that DAG's
   module docstring.
3. **MLflow registry writes are not transactional with the framework
   database.** `RetrainingWorkflow` registers and stages a version in
   MLflow (`tracking/mlflow_registry.py`) inside the same block that
   later commits the framework's own rows. If that transaction rolls
   back — a crash between promotion and `commit()` — the framework
   correctly keeps the previous model in PRODUCTION, but MLflow retains
   the registered version, so the two registries disagree until the next
   successful run. The framework side is the source of truth and stays
   safe; the MLflow side needs manual tidying. Observed in practice, not
   theoretical.
4. **Airflow "cancel" deletes the DAG run.** Airflow 2.x has no clean
   REST endpoint to cancel a running DAG run; deletion is the
   documented workaround (`AirflowOrchestrator.cancel_execution`).
5. **`TrainingService.wait_for_completion` polls** rather than using a
   callback/event bus.
6. **MLflow is optional.** The framework never requires it to import or
   run — `MLflowTracker` fails with a clear framework-level error if
   `mlflow` isn't installed, and `InMemoryTracker` is a drop-in for
   tests.
7. **`CONSOLE_WRITE_TOKEN` is still accepted, and is not
   authentication.** Scoped API keys are now the real credential (see
   [Authentication](#authentication)); the shared secret is kept because
   it is what every existing deployment — the Airflow DAG included — is
   configured with, and removing it in the same change that introduced
   keys would break all of them at once. A request authenticated that
   way still records the unverified `X-Actor` header as its actor.
   Migrate the DAG and any scripts to keys, then unset it.
8. **There is no browser login.** The console prompts for a credential
   and keeps it in `sessionStorage` for the tab. Read endpoints are
   ungated, so the console renders for anyone who can reach it —
   gating GETs needs session management the app does not have.

## Feature specifications

`docs/specs/` holds one specification per feature — the problem it
solves, its data model, the invariants a careless change would break,
and *why* it is built that way rather than some other way. Written for
whoever edits the framework next; this README covers using it.

Start at [`docs/specs/README.md`](docs/specs/README.md), which also
documents the conventions that recur across features: managers only
`flush()`, state machines instead of direct assignment, lazily-imported
adapters, the SAVEPOINT behind "never raises", and why the framework
never reads dataset files itself.

## License

MIT
