# Architecture

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

## Dependency direction

The framework depends only on its own abstractions (`Orchestrator`,
`ExperimentTracker`). Airflow and MLflow live in adapter modules and are
imported lazily — the framework stays importable (and testable) without
them installed. The Airflow image itself never imports `mlops_framework`
either (see the [Closed-Loop Demo](../demos/closed-loop-demo.md)) —
Airflow 2.10.4 pins `SQLAlchemy==1.4.x` internally, incompatible with the
framework's own `Mapped`/`mapped_column` models (SQLAlchemy 2.0+), so
the DAG talks to the framework over HTTP
(`mlops_framework.api.routers.internal`) instead.

## Module layout

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

See [Project Structure](../reference/project-structure.md) for the
repository layout above `src/`, and
[Database Schema](../reference/database-schema.md) for how the ORM
models in `database/models/` relate to each other.
