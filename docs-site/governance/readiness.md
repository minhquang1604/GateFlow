# Dataset Readiness

Whether a dataset version is fit to train on at all — enough rows,
fresh enough, the right columns, an acceptable missing-value ratio —
evaluated against an explicit `TrainingPolicy` and persisted.

```python
from mlops_framework.readiness import ReadinessEngine, TrainingPolicy

result = ReadinessEngine(session).evaluate(
    dv,
    TrainingPolicy(required_size=1000, freshness_hours=24,
                    required_columns=["amount", "is_fraud"],
                    dtypes={"amount": "float64", "is_fraud": "int64"}),
)
assert result.is_ready   # or result.status == "BLOCKED"; result.reasons is explainable
```

A `READY`/`BLOCKED` verdict is never a bare boolean — `result.reasons`
names exactly which checks failed, and the evaluation is persisted
(`ReadinessEvaluation`) so a blocked run has a record explaining why.

## Where it's exposed

| Interface | |
|---|---|
| HTTP | `GET /api/readiness/{version_id}` |
| Console | The dataset detail page's readiness panel |

## What comes after readiness

Readiness alone doesn't mean training *should* happen right now — that
is a separate question. See [Training Eligibility](eligibility.md).
