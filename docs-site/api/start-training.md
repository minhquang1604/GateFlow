# Starting a Training Run

```bash
curl -X POST localhost:8000/api/training-runs \
  -H "X-Console-Token: $CONSOLE_WRITE_TOKEN" -H "X-Actor: alice" \
  -d '{"dataset_version_id": 4,
       "training_entrypoint": "case_studies.fraud_detection.pipelines:train_xgboost",
       "model_name": "fraud-xgboost"}'
# 202 {"training_run_id": 17, "status": "RUNNING", "execution_id": "..."}
```

Or **Train now** on a version in the [Gateflow console](../console.md).
Training was previously startable only from Python (`project.train`) or
through `/api/internal/*`, which is the DAG's own callback surface —
reachable, but not something a console button should be calling.

## Why creating and starting are one call

A created-but-never-started run is a `PENDING` row that reads as a stuck
training run to everyone looking at `/runs`, and no caller wants one.
The run is committed before the trigger, because the DAG resolves it by
calling `GET /internal/training-runs/{id}/context` back over HTTP — a
separate transaction, which cannot see an uncommitted row. If Airflow
then refuses the run, it is marked `FAILED` rather than left `PENDING`
for nothing to close.

## Why `training_entrypoint` is a separate field

`pipeline_id` means a *dag_id* to `AirflowOrchestrator`, and the
`module:callable` the DAG actually runs is a separate thing that
travels in the run's metadata — see
[`pipeline_id` means different things](../concepts/training.md#airfloworchestrator-vs-localdockerorchestrator-pipeline_id-means-different-things).
Leaving `model_name` unset trains and reports but registers no
`ModelVersion` — the right default for an exploratory run started by
hand.
