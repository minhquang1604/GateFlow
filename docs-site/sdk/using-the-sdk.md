# Using the SDK

`MLOpsProject` is the entry point application code should use — it
never touches a manager, a database model, or an orchestrator directly.
The framework's own [case studies](../demos/fraud-detection.md) prove
the boundary holds via a static AST test.

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
explicitly to point at real Airflow/MLflow (see the
[Real End-to-End Demos](../demos/overview.md) for a full example).

## Next

- [Datasets & Versions](../concepts/datasets.md), [Model Registry](../concepts/models.md),
  [Lineage](../concepts/lineage.md) — the underlying concepts each SDK
  call touches.
- [Using the Framework Directly](framework-directly.md) — for framework
  contributors, or application code that needs governance primitives
  the SDK doesn't expose yet.
