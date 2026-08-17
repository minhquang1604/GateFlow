# Datasets & Versions

A `Dataset` is a logical name (`"credit-card-fraud"`); a `DatasetVersion`
is an **immutable** snapshot of that data — checksum, schema hash, row
count, and (since dataset versions can now be built by extending an
earlier one) an optional `parent_version_id`.

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from mlops_framework import DatasetManager

engine = create_engine("sqlite:///./mlops.db")
session = sessionmaker(bind=engine)()

dm = DatasetManager(session)
dataset = dm.create_dataset(name="fraud-detection", description="Credit card fraud data")
version = dm.create_version(
    dataset_id=dataset.id,
    storage_uri="s3://bucket/data/v1.csv",
    row_count=100_000,
    metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
)
session.commit()
```

Most application code should go through the SDK instead — see
[Using the SDK](../sdk/using-the-sdk.md):

```python
dataset = project.create_dataset("credit-card-transactions")
version = dataset.create_version(storage_uri="s3://bucket/v1.parquet", row_count=284_807)
```

## Extending a version rather than replacing it

`create_version` accepts `parent_version_id`, so a version built by
extending an earlier one — the retraining case, where V2 is V1 plus the
production data that drifted — records that lineage rather than looking
like it arrived from nowhere. See
[Lineage](lineage.md) for how that shows up in the graph, and the
[Closed-Loop Demo](../demos/closed-loop-demo.md#how-dataset-v2-is-built)
for a worked example of building V2 this way.

## Readiness

A dataset version existing does not mean it is ready to train on —
that's a separate, explicit decision. See
[Dataset Readiness](../governance/readiness.md).
