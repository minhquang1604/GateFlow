# Automated Retraining Workflow

One call chains readiness → drift (if `drift_service` + reference/current
data are given) → eligibility → human approval (if a gate is given) →
training → promotion → event:

```python
from mlops_framework.workflow import RetrainingWorkflow
from mlops_framework.events import InMemoryEventPublisher

workflow = RetrainingWorkflow(session, training_service=service, event_publisher=InMemoryEventPublisher())
outcome = workflow.run(
    dataset_version=dv, model=model,
    training_policy=TrainingPolicy(required_size=1000),
    eligibility_config=EligibilityConfig(cooldown_hours=12),
    promotion_config=PromotionConfig(min_metrics={"f1": 0.85}),
    pipeline_id="my_pkg.pipelines:train",   # LocalDockerOrchestrator convention
)
if outcome.promoted:
    print(outcome.steps)   # every step's pass/fail + explainable detail
```

`outcome.steps` is the whole point: every stage — `readiness`, `drift`,
`eligibility`, `approval`, `training`, `promotion`, `event` — reports
`passed`/`detail` whether it succeeded or blocked, so "why didn't this
retrain" is always answerable without reading logs.

## Works with Airflow too

Pass the DAG id as `pipeline_id` and the real callable separately as
`training_entrypoint` — see
[`pipeline_id` means different things](../concepts/training.md#airfloworchestrator-vs-localdockerorchestrator-pipeline_id-means-different-things):

```python
outcome = workflow.run(
    dataset_version=dv, model=model,
    training_policy=TrainingPolicy(required_size=1000),
    promotion_config=PromotionConfig(min_metrics={"f1": 0.85}),
    pipeline_id="mlops_training_pipeline",   # the Airflow dag_id
    training_entrypoint="my_pkg.pipelines:train",
    training_timeout=600.0,   # a real DAG run needs far longer than the 60s default
)
```

The framework side of this — surfacing the pipeline's metrics back to
the workflow, and not racing the DAG's own callbacks to close out the
run — is handled automatically; see
[Known Limitations](../operations/known-limitations.md) for the one
thing that still needs the DAG itself to cooperate.

## Two more knobs worth knowing about

`run(drift_config=...)` lets a caller pin the exact drift configuration
this run's own drift step uses — useful when a caller has already
measured drift elsewhere under a specific threshold/correction and
wants the workflow's internal check to agree, rather than silently
using a different one. `run(trigger_type=...)` overrides the
`TrainingRun.trigger_type` the workflow would otherwise infer from its
own drift step — worth setting explicitly when the run's real trigger
(a drift event on a *different* comparison, say) doesn't match what the
workflow's own internal drift check happens to see. The
[Closed-Loop Demo](../demos/closed-loop-demo.md#why-the-workflows-own-drift-check-reports-no-drift)
walks through exactly this case.

## Human approval

`RetrainingWorkflow` also takes an optional `approval_gate` — see
[Human Approval](approval.md).

## See it run end to end

The [Closed-Loop Demo](../demos/closed-loop-demo.md) drives every stage
of this workflow against the real stack, including a human denying a
retrain and the production model correctly staying untouched.
