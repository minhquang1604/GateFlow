# Training Eligibility

Separates "the data is READY" from "training should happen right now" —
cooldowns, minimum new rows, and (critically) whether drift was
actually observed.

```python
from mlops_framework.governance.eligibility import TrainingEligibilityPolicy, EligibilityConfig

policy = TrainingEligibilityPolicy(session)
decision = policy.evaluate(
    policy.build_context(dataset_version=dv, readiness=result, drift=drift_result, model=model),
    EligibilityConfig(require_drift_to_retrain=True, cooldown_hours=12),
)
if not decision.eligible:
    print(decision.reasons)
```

`require_drift_to_retrain=True` is what turns an automated retraining
workflow into a genuine *reaction* to drift, not a cron job that always
retrains — with no drift, this refuses.

!!! note "Eligibility is about the trigger, not the training set"
    Eligibility asks "should a retrain happen because of *this*
    dataset version and *this* drift measurement" — it is not required
    to be the same comparison the training set was built from. See the
    [Closed-Loop Demo's note on this exact subtlety](../demos/closed-loop-demo.md#why-the-workflows-own-drift-check-reports-no-drift)
    if you're gating a retrain on a merged dataset that already
    contains the reference population.

## Next

- [Human Approval](approval.md) — an eligible retrain isn't necessarily
  one you want to run unattended.
- [Automated Retraining Workflow](retraining-workflow.md) — where this
  fits in the chain.
