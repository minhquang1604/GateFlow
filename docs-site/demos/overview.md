# Real End-to-End Demos

Three entry points, each proving a different slice against the
**real** Docker Compose stack — no mocks. All assume
`docker compose --env-file .env.docker up -d` has been run (see
[Quickstart](../getting-started/quickstart.md)) and, when run from the
host rather than inside a container, that MLflow/MinIO/Airflow env
vars are set (already present in `.env.example` — `cp .env.example
.env` covers a host run).

| Script | Proves | Orchestrator |
|---|---|---|
| `scripts/run_end_to_end_demo.py` | The full governance chain — dataset → readiness → eligibility → training → promotion → serving reload → lineage — on a synthetic dataset | `AirflowOrchestrator` (real DAG) |
| `scripts/run_fraud_detection_e2e.py` | The same chain on the **real** 284,807-row Kaggle credit-card-fraud dataset; gates promotion on `average_precision` (the metric that actually matters at a 0.17% positive rate) | `LocalDockerOrchestrator` (training) + `AirflowOrchestrator` (adapter proof only) |
| `demo/run_closed_loop_demo.py` | **The closed loop**, end to end: Dataset V1 → Model V1 → production monitoring (a baseline window that must *not* flag) → a controlled covariate shift → real KS-test detection → Telegram approval → Dataset V2 = V1 + the drifted data → retrain → validate → archive V1 → promote V2. See the [Closed-Loop Demo](closed-loop-demo.md). | `AirflowOrchestrator` (real DAG) for **both** training runs — the generated datasets live on a bind mount both sides can read |

```bash
# 1. Full governance chain (synthetic data, ~5s)
docker compose --env-file .env.docker --profile demo run --rm demo

# 2. Real Kaggle dataset (needs case_studies/fraud_detection/data/creditcard.csv —
#    see the Fraud Detection case study to download it)
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

## Case studies

Two self-contained apps at the repo root, consuming the framework
through the SDK only:

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

`case_studies/*/tests/test_use_case.py::TestNoDirectManagerImports` is
a static AST check that fails CI if either case study ever imports a
manager, service, database model, or orchestrator directly — the SDK
boundary is enforced, not just documented. See
[Fraud Detection](fraud-detection.md) and
[Customer Churn](customer-churn.md).
