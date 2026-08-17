# MLOps Framework

A reusable MLOps framework for managing datasets, training runs, model
lifecycle, experiment tracking, and pipeline orchestration — agnostic to
any specific orchestrator or experiment tracker. Ships with a management
console called **Gateflow** (dashboard, datasets, runs, models,
pipelines, lineage), a Python SDK, an HTTP API, and two runnable case
studies that prove the abstractions hold up on real, different problems.

```python
from mlops_framework.sdk import MLOpsProject

project = MLOpsProject.with_defaults("fraud-detection")
project.register_pipeline("xgboost-training", "my_pkg.pipelines:train_xgb")

dataset = project.create_dataset("credit-card-transactions")
version = dataset.create_version(storage_uri="s3://bucket/v1.parquet", row_count=284_807)

run = project.train(dataset_version=version, pipeline="xgboost-training", wait=True)
print(run.status, run.metrics)
```

[:octicons-rocket-24: Quickstart](getting-started/quickstart.md){ .md-button .md-button--primary }
[:octicons-mark-github-16: View source](https://github.com/minhquang1604/GateFlow){ .md-button }

## What it does

- **Dataset Management** — logical datasets with immutable versioning
- **Dataset Versioning** — checksums, schema hashes, row counts, content-hash pinning
- **Training Run Lifecycle** — strict state machine (PENDING → RUNNING → SUCCESS / FAILED / CANCELLED)
- **Orchestration** — pluggable orchestrators (local subprocess, Airflow, …)
- **Experiment Tracking** — pluggable trackers (MLflow, in-memory, …)
- **Model Registry** — `Model` and `ModelVersion` with a promotion lifecycle, and a **rollback** path back to a known-good version
- **Lineage** — full chain: DatasetVersion → TrainingRun → ModelVersion → ServingInstance, every version of a dataset shown in parallel
- **Dataset Readiness** — explainable READY/BLOCKED decisions, persisted
- **Training Eligibility** — separates "data is ready" from "training should happen now"
- **Drift Detection** — pluggable `DriftDetector` ABC, scipy KS-test / chi-square reference implementation
- **Promotion Policy** — explicit, explainable APPROVED/REJECTED, configurable per call
- **Automated Retraining** — one framework-controlled workflow chaining readiness → drift → eligibility → training → promotion
- **Model Promotion Events** — `EventPublisher` ABC with HTTP and in-memory adapters
- **Human Approval** — pluggable `ApprovalGate` ABC (Telegram reference adapter), so an automated retrain can block on a real person; denies by default
- **Serving Bridge** — FastAPI app that atomically reloads a promoted model
- **Gateflow** — a server-rendered management console (no build step) over all of the above
- **Python SDK** — `MLOpsProject`, so application code never imports a manager directly

## Find your way around

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **New here?**

    ---

    Bring up the full stack with Docker Compose and watch the
    governance chain run end to end.

    [:octicons-arrow-right-24: Quickstart](getting-started/quickstart.md)

-   :material-sitemap:{ .lg .middle } **How is it built?**

    ---

    The abstractions the framework depends on, and how they compose
    into `RetrainingWorkflow`.

    [:octicons-arrow-right-24: Architecture](concepts/architecture.md)

-   :material-shield-check:{ .lg .middle } **Governance**

    ---

    Readiness, drift detection, eligibility, promotion policy, human
    approval — the decisions behind every retrain.

    [:octicons-arrow-right-24: Governance overview](governance/index.md)

-   :material-language-python:{ .lg .middle } **Using the SDK**

    ---

    `MLOpsProject` — the entry point application code should use.
    Never touches a manager or a database model directly.

    [:octicons-arrow-right-24: SDK guide](sdk/using-the-sdk.md)

-   :material-api:{ .lg .middle } **HTTP API**

    ---

    Authentication, scopes, and the REST endpoints Gateflow itself is
    built on.

    [:octicons-arrow-right-24: API reference](api/reference.md)

-   :material-monitor-dashboard:{ .lg .middle } **Gateflow console**

    ---

    The server-rendered management UI — dashboard, datasets, runs,
    models, lineage, activity.

    [:octicons-arrow-right-24: Console tour](console.md)

-   :material-play-circle:{ .lg .middle } **See it run**

    ---

    Three real end-to-end demos against the real stack, including a
    full closed-loop drift → approval → retrain → promote story.

    [:octicons-arrow-right-24: Demos & case studies](demos/overview.md)

-   :material-cog:{ .lg .middle } **Running it yourself**

    ---

    Environment variables, database migrations, testing, and known
    limitations.

    [:octicons-arrow-right-24: Deployment & operations](operations/configuration.md)

</div>

## Feature specifications

For *why* the framework is built the way it is — the problem each
feature solves, its data model, the invariants a careless change would
break — see [`docs/specs/`](https://github.com/minhquang1604/GateFlow/tree/main/docs/specs)
in the repository. That tree is written for whoever edits the framework
next; this site covers using it.

## License

MIT.
