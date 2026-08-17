# Fraud Detection Case Study

Demonstrates that the MLOps Framework can be used **without ever
importing a manager, service, or database model directly** — only the
public SDK.

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

```text
Run 1 finished with status=SUCCESS; pipeline_id=case_studies.fraud_detection.pipelines:train_baseline
Lineage node types: ['DatasetVersion', 'TrainingRun']
```

## Data

Two sources, one column contract. `data.normalize_columns` maps either
onto the canonical lower-case names (`time`, `amount`, `v1`..`v28`,
`class`), so the schema hash — and therefore every readiness check — is
identical whichever you train on.

| Source | Rows | Fraud | Header | Committed |
|---|---|---|---|---|
| `data/creditcard.csv` | 284,807 | 492 (0.173%) | `Time,V1..V28,Amount,Class` | no — 144 MB |
| `data.generate()` / `write_csv()` | configurable | configurable | `time,amount,v1..v28,class` | n/a, generated |

### Getting the real dataset

`creditcard.csv` is the [Kaggle Credit Card Fraud Detection][kaggle]
dataset: 48 hours of European card transactions, features `V1`..`V28`
already PCA-anonymised. At 144 MB it is past GitHub's 100 MB per-file
limit, so it is gitignored and downloaded rather than committed:

```bash
# needs a Kaggle account + ~/.kaggle/kaggle.json
kaggle datasets download -d mlg-ulb/creditcardfraud \
  -p case_studies/fraud_detection/data --unzip
```

[kaggle]: https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud

The tests never touch it — they use the synthetic generator, so the
suite stays hermetic and fast.

## Training on the real data through the framework

```bash
MLFLOW_TRACKING_URI=http://<host>:5000 \
AIRFLOW_BASE_URL=http://<host>:8080 \
AIRFLOW_USERNAME=admin AIRFLOW_PASSWORD=... \
python -m scripts.run_fraud_detection_e2e
```

That script registers the dataset version (row count and schema read
off the file, plus a SHA-256 of its bytes), runs the readiness engine,
trains `train_xgboost` through `LocalDockerOrchestrator`, logs to a
real MLflow server, and applies the promotion policy. Read its module
docstring for what it does and does not claim about Airflow.

### Why the promotion policy gates on `average_precision`

At a 0.173% positive rate, a model that never predicts fraud still
scores about 0.95 ROC-AUC — the metric is dominated by the 284,315
negatives. Average precision (area under the precision-recall curve)
stays near the 0.0017 base rate for that same useless model, so it is
the metric that actually distinguishes a working classifier here. The
pipeline reports both; the policy gates on the former.

## Running the real end-to-end demo

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
- fits `xgboost.XGBClassifier` with `scale_pos_weight` to handle class
  imbalance,
- logs `f1`, `precision`, `recall`, `roc_auc` to MLflow,
- persists a pickled model artifact via `mlflow.log_artifact`,
- returns a dict shaped like the other pipelines so the framework's
  promotion policy can evaluate it.

## Reusability evidence

The case study uses two different pipelines (`fraud-baseline` and
`fraud-advanced`) on the same dataset, demonstrating that the SDK is
not hard-coded to a single pipeline or metric. See the
[Customer Churn](customer-churn.md) case study for a completely
different domain using the same SDK.
