# Drift Detection

Compares a production data sample against a training reference and
decides — per feature, and overall — whether the distribution has
shifted enough to matter.

```python
from mlops_framework.drift import ScipyDriftDetector, DriftService, DriftConfig

service = DriftService(session, ScipyDriftDetector())   # KS-test (numeric) / chi-square (categorical)
result = service.evaluate(
    reference_version=ref_dv, current_version=cur_dv,
    reference_data={"amount": [...]}, current_data={"amount": [...]},
    config=DriftConfig(threshold=0.05),
)
if result.drift_detected:
    print([f.feature for f in result.feature_results if f.drift_detected])
```

Every evaluation is persisted (`DriftEvaluation`) and browsable per
dataset version in Gateflow, and via `GET /api/drift/{version_id}`.

## The framework never reads dataset files

`DriftService` takes feature values from its caller — nothing under
`src/` opens an S3 object or a CSV. Running a check against real data
therefore goes through an Airflow DAG that reads the data and posts
sampled values back; see
[Running a Drift Check](../api/drift-check.md) for that flow and why
it's built that way.

## Correcting for many features at once

`DriftConfig.correction` controls how the per-feature threshold is
adjusted when many features are tested in one evaluation:

| `correction` | Behaviour |
|---|---|
| `"none"` (default) | Every feature is tested at `threshold`; drift is declared if *any* one is significant. Fine for a handful of features. |
| `"bonferroni"` | `threshold` is divided by the number of features actually tested, holding the **family-wise** false-positive rate at `threshold` instead. |

Across *n* independent tests at α = 0.05, the probability of at least
one false positive is `1 - 0.95ⁿ` — 40% at ten features, ~79% at
thirty. A monitor that cries drift four times out of five is worse than
none, because the correct response to it is to stop believing it.
`"none"` stays the default for backwards compatibility; opt into
`"bonferroni"` for anything with more than a handful of monitored
features. The [Closed-Loop Demo](../demos/closed-loop-demo.md#drift-detection-and-why-it-is-corrected)
measures the effect on a real 29-feature dataset.

## What happens after drift is detected

Detecting drift doesn't retrain anything by itself. See
[Training Eligibility](eligibility.md) (which can require drift before
allowing a retrain) and [Automated Retraining Workflow](retraining-workflow.md).
