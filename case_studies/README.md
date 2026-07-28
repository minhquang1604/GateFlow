# Case Studies

Two self-contained apps that exercise the MLOps Framework through its
public SDK. They live at the repository root, not under
`src/mlops_framework/`, so they are **consumers** of the framework —
exactly like a third-party app would be.

| | Fraud Detection | Customer Churn |
|---|---|---|
| Folder | [`fraud_detection/`](fraud_detection/) | [`customer_churn/`](customer_churn/) |
| Domain | Predict fraudulent credit-card transactions | Predict 30-day telecom churn |
| Data shape | 30 numeric + binary class | 4 numeric + 2 categorical + binary class |
| Pipelines | `fraud-baseline`, `fraud-advanced` | `churn-baseline`, `churn-balanced` |
| Metrics | `f1`, `roc_auc` | `accuracy`, `f1`, `recall` |
| **SDK imports in `app.py`** | `mlops_framework.sdk` | `mlops_framework.sdk` |

## Reusability claim

Both case studies:

* Use the **same** SDK entry point: `MLOpsProject.with_defaults(name)`
* Use the **same** pipeline registration: `project.register_pipeline(name, id)`
* Use the **same** lifecycle: `create_dataset → create_version → train → read lineage`
* Use the **same** test scaffolding: in-memory SQLite + local orchestrator
* Touch **no** manager, service, database model, or orchestrator class

The last point is enforced by a static AST check in each case study's
`tests/test_use_case.py::TestNoDirectManagerImports`. If anyone in the
future imports a manager directly, the test fails and CI catches it.

## Running

```bash
# Fraud Detection
python -m case_studies.fraud_detection.app

# Customer Churn
python -m case_studies.customer_churn.app
```

## Tests

```bash
pytest case_studies/
```

The case study tests are part of the main test suite
(`testpaths = ["tests", "case_studies"]` in `pyproject.toml`) and run
in CI alongside the unit and integration tests.
