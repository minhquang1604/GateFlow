# Fraud Detection Case Study

Demonstrates that the MLOps Framework can be used **without ever importing
a manager, service, or database model directly** — only the public SDK.

## Layout

```
fraud_detection/
├── __init__.py
├── app.py          # User-facing entry point (uses SDK only)
├── data.py         # Synthetic data generator + DataFrame loader
├── pipelines.py    # Orchestrator-callable training pipelines (3 of them)
└── tests/
    └── test_use_case.py
```

## Pipelines

The case study ships three pipelines registered as friendly names in
the demo:

| Pipeline id | Use | Notes |
|---|---|---|
| `case_studies.fraud_detection.pipelines:train_baseline` | Hermetic tests | Pure-Python, no ML libs |
| `case_studies.fraud_detection.pipelines:train_advanced` | Hermetic tests | Pure-Python, no ML libs |
| `case_studies.fraud_detection.pipelines:train_xgboost` | Real end-to-end demo | Real XGBoost + sklearn; logs to MLflow if a `tracker_run_id` is in the conf dict |

## Running the hermetic demo

```bash
python -m case_studies.fraud_detection.app
```

Expected output:

```
Run 1 finished with status=SUCCESS; pipeline_id=case_studies.fraud_detection.pipelines:train_baseline
Lineage node types: ['Dataset', 'DatasetVersion', 'TrainingRun']
```

## Running the real end-to-end demo (Week 5)

```bash
docker compose --env-file .env.docker up -d
docker compose --env-file .env.docker run --rm app alembic upgrade head
docker compose --env-file .env.docker --profile demo run --rm demo
```

The one-shot `demo` service exercises the full governance chain with
**real MLflow**, **real Airflow**, and the **real XGBoost trainer**
shipped in this case study:

- `train_xgboost` reads `data/transactions.csv`, splits 80/20 with
  `sklearn.model_selection.train_test_split`,
- fits `xgboost.XGBClassifier` with `scale_pos_weight` to handle
  class imbalance,
- logs `f1`, `precision`, `recall`, `roc_auc` to MLflow,
- persists a pickled model artifact via `mlflow.log_artifact`,
- returns a dict shaped like the other pipelines so the framework's
  promotion policy can evaluate it.

## Reusability evidence

The case study uses two different pipelines (``fraud-baseline`` and
``fraud-advanced``) on the same dataset, demonstrating that the SDK is
not hard-coded to a single pipeline or metric. See the
[Customer Churn case study](../customer_churn/) for a completely
different domain using the same SDK.
