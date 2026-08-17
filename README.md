<h1 align="center" style="border-bottom: none">
    <a href="https://minhquang1604.github.io/Gateflow/">
        <img alt="Gateflow logo" src="https://raw.githubusercontent.com/minhquang1604/Gateflow/main/docs/gateflow-logo.svg" width="120" />
    </a>
</h1>
<h2 align="center" style="border-bottom: none">A governed MLOps framework for datasets, training, and automated retraining</h2>

Gateflow is a reusable MLOps framework for teams that need every model promotion to be **explainable** and every retrain to be **audited** — not just automated. It chains dataset readiness, drift detection, training eligibility, human approval, and promotion policy into one framework-controlled workflow, agnostic to any specific orchestrator or experiment tracker. Ships with a management console, a Python SDK, an HTTP API, and a full [closed-loop demo](https://minhquang1604.github.io/Gateflow/demos/closed-loop-demo/) that proves the whole lifecycle end to end against a real Postgres, MLflow, and Airflow stack — no mocks.

<div align="center">

[![License](https://img.shields.io/github/license/minhquang1604/Gateflow)](https://github.com/minhquang1604/Gateflow/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![Docs](https://github.com/minhquang1604/Gateflow/actions/workflows/docs.yml/badge.svg)](https://minhquang1604.github.io/Gateflow/)

</div>

<div align="center">
   <div>
      <a href="https://minhquang1604.github.io/Gateflow/"><strong>Docs</strong></a> ·
      <a href="https://minhquang1604.github.io/Gateflow/getting-started/quickstart/"><strong>Quickstart</strong></a> ·
      <a href="https://minhquang1604.github.io/Gateflow/demos/closed-loop-demo/"><strong>Closed-Loop Demo</strong></a> ·
      <a href="https://minhquang1604.github.io/Gateflow/demos/overview/"><strong>Case Studies</strong></a> ·
      <a href="docs/specs/README.md"><strong>Feature Specs</strong></a>
   </div>
</div>

<br>

```python
from mlops_framework.sdk import MLOpsProject

project = MLOpsProject.with_defaults("fraud-detection")
project.register_pipeline("xgboost-training", "my_pkg.pipelines:train_xgb")

dataset = project.create_dataset("credit-card-transactions")
version = dataset.create_version(storage_uri="s3://bucket/v1.parquet", row_count=284_807)

run = project.train(dataset_version=version, pipeline="xgboost-training", wait=True)
print(run.status, run.metrics)
```

## Get Started in 3 Simple Steps

From zero to the full governance chain running against real services in minutes. [Full Quickstart →](https://minhquang1604.github.io/Gateflow/getting-started/quickstart/)

> **No Docker? Lighter path — SQLite, no external services**
>
> ```bash
> pip install -e ".[dev]"
> echo 'DATABASE_URL=sqlite:///./mlops.db' >> .env && alembic upgrade head
> uvicorn mlops_framework.api.app:create_app --factory --reload
> ```
>
> Gateflow comes up at `http://localhost:8000`. Full instructions: [Installation →](https://minhquang1604.github.io/Gateflow/getting-started/installation/)

**1. Bring up the stack**

```bash
git clone https://github.com/minhquang1604/Gateflow.git && cd Gateflow
cp .env.example .env.docker
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker run --rm app alembic upgrade head
```

**2. Run the governed retraining chain**

```bash
# dataset → readiness → eligibility → real Airflow DAG → promotion → serving reload → lineage
docker compose --env-file .env.docker --profile demo run --rm demo
```

**3. Inspect what happened**

| URL | What you'll see |
|---|---|
| `http://localhost:8000` | **Gateflow** — dashboard, the promoted model, its metrics, its lineage graph |
| `http://localhost:5000` | MLflow — the real training run: params, metrics, logged artifact |
| `http://localhost:8080` | Airflow — the DAG run that actually executed the training (`airflow`/`airflow`) |
| `http://localhost:9001` | MinIO console — the uploaded model artifact (`minioadmin`/`minioadmin`) |

## Core Capabilities

**Dataset & Training Lifecycle** — logical datasets with immutable, checksummed versions; a strict training-run state machine (`PENDING → RUNNING → SUCCESS/FAILED/CANCELLED`); pluggable orchestrators (local subprocess, Airflow) and experiment trackers (MLflow, in-memory) behind one abstraction, so application code never imports either directly.
[Datasets →](https://minhquang1604.github.io/Gateflow/concepts/datasets/) · [Training →](https://minhquang1604.github.io/Gateflow/concepts/training/)

**Governance & Automated Retraining** — explainable READY/BLOCKED dataset readiness; drift detection (scipy KS-test / chi-square, with a Bonferroni correction for the many-features false-positive problem); training eligibility that separates "data is ready" from "retrain now"; an explicit, configurable promotion policy; a human `ApprovalGate` that can block an automated retrain on a real person and denies by default. `RetrainingWorkflow` chains all five into one framework-controlled call.
[Governance overview →](https://minhquang1604.github.io/Gateflow/governance/)

**Lineage & Reproducibility** — the full chain, `DatasetVersion → TrainingRun → ModelVersion → ServingInstance`, with every version of a dataset shown in parallel rather than a single disconnected slice; a self-contained reproducibility report (markdown or HTML) for any model version, citing its exact dataset content hash and governance trail.
[Lineage →](https://minhquang1604.github.io/Gateflow/concepts/lineage/)

**Management Console & API** — **Gateflow**, a server-rendered console (no build step) over datasets, runs, models, pipelines, and lineage; a full REST API with scoped API-key authentication; a Python SDK (`MLOpsProject`) so application code never touches a manager or a database model directly — enforced by a static AST test across two real, different case studies.
[Console tour →](https://minhquang1604.github.io/Gateflow/console/) · [API reference →](https://minhquang1604.github.io/Gateflow/api/reference/) · [SDK guide →](https://minhquang1604.github.io/Gateflow/sdk/using-the-sdk/)

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

The framework depends only on its own abstractions — Airflow, MLflow, and Telegram live in adapter modules, imported lazily, so the framework stays importable and testable without any of them installed. Full module layout, dependency direction, and why the Airflow DAG talks to the framework over HTTP rather than importing it: [Architecture →](https://minhquang1604.github.io/Gateflow/concepts/architecture/)

## See It Run

Three entry points prove different slices against the **real** stack — no mocks:

| | Proves | Orchestrator |
|---|---|---|
| [`scripts/run_end_to_end_demo.py`](scripts/run_end_to_end_demo.py) | The full governance chain on a synthetic dataset | `AirflowOrchestrator` (real DAG) |
| [`scripts/run_fraud_detection_e2e.py`](scripts/run_fraud_detection_e2e.py) | The same chain on the real 284,807-row Kaggle credit-card-fraud dataset | `LocalDockerOrchestrator` + `AirflowOrchestrator` |
| [`demo/run_closed_loop_demo.py`](demo/README.md) | **The closed loop**: drift detected → Telegram approval → Dataset V2 = V1 + drifted data → retrain → validate → archive V1 → promote V2 | `AirflowOrchestrator` for both training runs |

Run `--decision reject` on the closed-loop demo at least once — it's the shorter, more convincing half of the story: drift is detected, the alert goes out, the admin says no, and *nothing else happens*. The production model is never replaced by a failed, rejected, or unapproved retrain. Full walkthrough, including the statistical evidence behind the drift correction: [Closed-Loop Demo →](https://minhquang1604.github.io/Gateflow/demos/closed-loop-demo/)

Two case studies prove the SDK boundary holds on real, different problems — fraud detection (30 numeric features) and customer churn (numeric + categorical) — with a static AST test that fails CI if either ever imports a manager directly. [Case studies →](https://minhquang1604.github.io/Gateflow/demos/overview/)

## Deployment

| | Use |
|---|---|
| SQLite, no Docker | Exploring the framework locally, no external services |
| Docker Compose | Full stack — Postgres, MinIO, MLflow, Airflow, Gateflow — for the real end-to-end demos |
| AWS ECS (Terraform) | Production, behind an ALB — see [`infrastructure/terraform/`](infrastructure/terraform/) |

Every state-changing endpoint requires either a scoped API key or a shared secret — see [Authentication →](https://minhquang1604.github.io/Gateflow/api/authentication/). Configuration reference, database migrations, testing, and known limitations: [Operations →](https://minhquang1604.github.io/Gateflow/operations/configuration/)

## Documentation

Full docs at **[minhquang1604.github.io/Gateflow](https://minhquang1604.github.io/Gateflow/)** — Getting Started, Concepts, Governance, the SDK, the HTTP API, the Gateflow console, demos and case studies, deployment and operations, and a full reference section.

For *why* the framework is built the way it is — the problem each feature solves, its data model, the invariants a careless change would break — see [`docs/specs/`](docs/specs/README.md), written for whoever edits the framework next.

## 💭 Support

Found a bug or have a feature request? [Open an issue](https://github.com/minhquang1604/Gateflow/issues). Questions about using the framework: check the [docs](https://minhquang1604.github.io/Gateflow/) first, then open a [discussion](https://github.com/minhquang1604/Gateflow/discussions).

## 🤝 Contributing

Issues and pull requests are welcome. There's no CONTRIBUTING guide yet — for anything beyond a small fix, open an issue first to discuss the approach before investing time in a PR.

## ⭐️ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=minhquang1604/Gateflow&type=Date)](https://star-history.com/#minhquang1604/Gateflow&Date)

## ✏️ Citation

```bibtex
@software{gateflow,
  author = {Cao Minh Quang},
  title = {Gateflow: A Governed MLOps Framework for Explainable, Automated Model Retraining},
  year = {2026},
  url = {https://github.com/minhquang1604/Gateflow}
}
```

## License

[MIT](LICENSE) — built by [Cao Minh Quang](https://github.com/minhquang1604).
