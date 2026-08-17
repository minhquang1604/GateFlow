# Running a Drift Check

```bash
curl -X POST localhost:8000/api/drift/12/check \
  -H "X-Console-Token: $CONSOLE_WRITE_TOKEN" -d '{}'
# 202 {"dag_id": "mlops_drift_check", "execution_id": "...", ...}
```

Or **Run check** on a version's drift panel in the
[Gateflow console](../console.md). The verdict appears on
`GET /api/drift/{id}` when the DAG finishes.

## Why this is queued on Airflow rather than run in-process

The framework does not read dataset files — `DriftService` takes
feature values from its caller, and nothing under `src/` opens an S3
object or a CSV — so neither the console nor the API process can compute
drift itself. Giving the app container S3 credentials and a 144 MB CSV
inside a 256 MiB reservation is the exact failure that already killed
Airflow's own gunicorn worker once, so the work goes where the data
already is: `mlops_drift_check` reads both versions, samples them, and
posts the values to `POST /api/internal/drift`.

The DAG does the I/O and nothing else. Which detector, which thresholds
(persisted settings unless overridden), whether it counts as drift, and
the `DriftEvaluation` row are all decided framework-side — a DAG that
computed its own verdict could assert anything, and the row would be a
client's claim rather than the framework's conclusion. Same split as
`resolve_context`/readiness in `mlops_training_pipeline.py`.

Sampling (default 5000 rows/feature) is about transport, not statistics:
a KS test settles on a few thousand points, so shipping hundreds of
thousands of values per feature over HTTP would reach the same answer
more slowly.

See [Drift Detection](../governance/drift.md) for what the resulting
evaluation actually measures.
