# Customer Churn Case Study

A second consumer of the MLOps Framework SDK, in a completely different
domain from [Fraud Detection](fraud-detection.md). The framework code
is the same; only the data shape, the pipeline, and the metric names
differ.

This case study is the **reusability proof**: a non-fraud,
non-ML-library, non-default-schema app uses the exact same SDK and
gets the same guarantees (datasets, versions, runs, lineage, models,
governance).

## Layout

```
customer_churn/
├── __init__.py
├── app.py          # User-facing entry point (uses SDK only)
├── data.py         # Synthetic churn data generator
├── pipelines.py    # Orchestrator-callable training pipelines
└── tests/
    └── test_use_case.py
```

## Running

```bash
python -m case_studies.customer_churn.app
```

## Reusability evidence

| Concern | Fraud Detection | Customer Churn |
|---|---|---|
| Data shape | 30 numeric features + binary class | 4 numeric + 2 categorical + binary class |
| Pipeline 1 | `fraud-baseline` | `churn-baseline` |
| Pipeline 2 | `fraud-advanced` | `churn-balanced` |
| Metrics | `f1`, `roc_auc` | `accuracy`, `f1`, `recall` |
| Model name | `fraud-xgboost` | `churn-classifier` |
| Task | binary_classification | binary_classification |
| **SDK import** | `mlops_framework.sdk` | `mlops_framework.sdk` |

The fact that *no other imports are needed* in either case study's
`app.py` is verified by the static check in
`tests/test_use_case.py::TestNoDirectManagerImports`.
