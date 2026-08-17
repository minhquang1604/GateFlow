# Model Registry

`Model` is a logical name (`"fraud-xgboost"`); a `ModelVersion` goes
through an explicit promotion state machine — nothing sets `state`
directly.

```python
from mlops_framework import ModelManager
from mlops_framework.database.models.model_version import ModelState

mm = ModelManager(session)
model = mm.create_model(name="fraud-model", task="fraud_detection")
mv = mm.create_model_version(
    model_id=model.id,
    dataset_version_id=version.id,
    training_run_id=run.id,
    metrics={"f1": 0.86, "roc_auc": 0.92},
    state=ModelState.CANDIDATE,
)
mm.transition_state(mv.id, ModelState.APPROVED)
mm.transition_state(mv.id, ModelState.PRODUCTION)
```

```
TRAINING   -> CANDIDATE | REJECTED
CANDIDATE  -> APPROVED  | REJECTED | PRODUCTION
APPROVED   -> PRODUCTION | ARCHIVED | REJECTED
PRODUCTION -> ARCHIVED
ARCHIVED | REJECTED   (terminal)
```

Exactly one `ModelVersion` per `Model` may be `PRODUCTION` at a time —
promoting a new one archives the incumbent first, so nothing downstream
can ever observe two.

## Deciding whether a candidate should be promoted

`ModelManager.transition_state` doesn't decide *whether* to promote —
that's `ModelPromotionPolicy`, a separate, explainable decision. See
[Promotion Policy](../governance/promotion.md).

## Getting a bad production model out

Promotion isn't the only way a `ModelVersion` reaches `PRODUCTION` —
rolling back to a previously-good version is the other path, and it
deliberately skips the promotion policy. See
[Rolling Back](../governance/rollback.md).

```python
model = project.get_model("fraud-xgboost")
for mv in model.versions:
    print(mv.version_number, mv.state, mv.metrics)
```
