# Experiment Tracking

Application code calls the framework's `ExperimentTracker` abstraction,
never `mlflow.*` directly — `InMemoryTracker` in tests, `MLflowTracker`
in production.

```python
from mlops_framework.tracking.in_memory import InMemoryTracker

tracker = InMemoryTracker()   # or MLflowTracker(tracking_uri=..., experiment_name=...)
tracker.start_run(run_name="exp-42", tags={"pipeline": "fraud"})
tracker.log_params({"n_estimators": 100, "max_depth": 6})
tracker.log_metrics({"f1": 0.86, "roc_auc": 0.92}, step=1)
tracker.log_artifact("model.pkl")
tracker.end_run(status="SUCCESS")
```

`MLflowTracker` is imported lazily — the framework stays importable (and
testable) without `mlflow` installed at all. See
[Known Limitations](../operations/known-limitations.md) for what that
means in practice.

## MLflow credentials

`MLflowTracker`'s *client* talks to the artifact store (MinIO/S3)
directly, bypassing the MLflow tracking server — so every process that
logs an artifact needs `MLFLOW_S3_ENDPOINT_URL` /
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` set, not just the `mlflow`
service itself. Missing them fails `log_artifact`/`download_artifacts`
with a silent `AccessDenied`, not a crash. See
[Configuration](../operations/configuration.md).

## Where the run this produces goes next

A `TrainingRun`'s tracker run id is what a `ModelVersion` stores as
`mlflow_run_id`, and it's also what the Gateflow console's run detail
page and the [REST API's MLflow proxy](../api/reference.md) read live
metrics and artifacts from.
