# Promotion Policy

Whether a candidate `ModelVersion` is good enough to become the
production model — explicit, explainable, configurable per call.

```python
from mlops_framework.governance.promotion import ModelPromotionPolicy, PromotionContext, PromotionConfig

decision = ModelPromotionPolicy().evaluate(
    PromotionContext(candidate=mv, production=production_mv),
    PromotionConfig(min_metrics={"f1": 0.85}, must_beat_production=True),
)
if decision.approved:
    mm.transition_state(mv.id, ModelState.APPROVED)
    mm.transition_state(mv.id, ModelState.PRODUCTION)
```

## `must_beat_production` vs an absolute floor

`must_beat_production` compares the candidate against production's
*stored*, training-time metrics on every metric they share. That stops
being a fair bar once the production model's real-world data has
drifted — pass `must_beat_production=False` with an absolute
`min_metrics` floor instead when retraining in reaction to drift. See
the [Closed-Loop Demo](../demos/closed-loop-demo.md#how-model-v2-is-validated),
which also re-scores the production model on the drifted data to show
why the stored number is not the bar.

## What promotion does, in order

1. `mm.transition_state(mv.id, ModelState.APPROVED)`
2. Archive the incumbent `PRODUCTION` version, if any
3. `mm.transition_state(mv.id, ModelState.PRODUCTION)`

Archiving before promoting (not after) is deliberate — nothing
downstream should ever be able to observe two `PRODUCTION` versions for
the same model at once, even for a moment.

## What happens on promotion

A successful promotion publishes a `ModelPromotedEvent` (see
[Serving Bridge](../concepts/serving.md)) so a live serving process can
reload atomically.

## Next

- [Automated Retraining Workflow](retraining-workflow.md) — the
  framework-controlled chain that ends in this decision.
- [Rolling Back](rollback.md) — the other, deliberately
  policy-free path back to `PRODUCTION`.
