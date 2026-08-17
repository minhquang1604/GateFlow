# Training Runs & Orchestration

## Training runs — strict lifecycle

Orchestrators and trackers go through `TrainingManager`; nothing mutates
the row directly.

```python
from mlops_framework import TrainingManager
from mlops_framework.exceptions import InvalidStatusTransitionError

tm = TrainingManager(session, dm)
run = tm.create_run(dataset_version_id=version.id, pipeline_id="fraud-training-pipeline")

tm.start_run(run.id)      # PENDING -> RUNNING
tm.complete_run(run.id)   # RUNNING  -> SUCCESS

try:
    tm.start_run(run.id)  # raises — terminal
except InvalidStatusTransitionError as exc:
    print(exc)
```

```
PENDING -> RUNNING -> SUCCESS | FAILED
PENDING -> CANCELLED
RUNNING -> CANCELLED
SUCCESS | FAILED | CANCELLED   (terminal)
```

## End-to-end training — same code, local or Airflow

`TrainingService` composes the orchestrator and the tracker. Swapping
`LocalDockerOrchestrator` for `AirflowOrchestrator` is the only line that
changes.

```python
from mlops_framework import TrainingManager, TrainingService, LocalDockerOrchestrator, InMemoryTracker

service = TrainingService(TrainingManager(session, dm), LocalDockerOrchestrator(), InMemoryTracker())
run = service.create_run(dataset_version_id=version.id, pipeline_id="my_pkg.pipelines:train")
service.start_run(run.id)
final_state = service.wait_for_completion(run.id)   # "SUCCESS" or "FAILED"
```

```python
from mlops_framework.orchestration.airflow import AirflowOrchestrator

orchestrator = AirflowOrchestrator(base_url="http://airflow.internal:8080", username="airflow", password="airflow")
# `pipeline_id` now means the DAG id, not "module:callable" — see
# "AirflowOrchestrator vs LocalDockerOrchestrator" below.
```

### `AirflowOrchestrator` vs `LocalDockerOrchestrator` — `pipeline_id` means different things

`LocalDockerOrchestrator.trigger_pipeline(pipeline_id, ...)` imports
`pipeline_id` directly as `"module:callable"`. `AirflowOrchestrator.trigger_pipeline(pipeline_id, ...)`
treats `pipeline_id` as the **Airflow `dag_id`** instead — the real
Python callable travels separately, in
`TrainingRun.metadata["training_entrypoint"]`, which the DAG's
`resolve_context`/`train` tasks read back over HTTP (see
`infrastructure/airflow/dags/mlops_training_pipeline.py`). Get this
backwards and `AirflowOrchestrator` 404s trying to trigger a DAG named
after your Python module path. `scripts/run_end_to_end_demo.py` and
`demo/steps/retrain.py` both show the correct pattern.

!!! tip "Starting a run without writing Python"
    The Gateflow console has a **Train now** button on any dataset
    version, and there's an HTTP endpoint too — see
    [Starting a Training Run](../api/start-training.md).

## Next

- [Model Registry](models.md) — what a successful training run produces.
- [Experiment Tracking](tracking.md) — where params, metrics, and
  artifacts actually get logged.
- [Automated Retraining Workflow](../governance/retraining-workflow.md)
  — the framework-controlled chain that creates, runs, and evaluates a
  training run automatically.
