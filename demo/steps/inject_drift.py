"""Phase 4 — inject a controlled, reproducible distribution shift.

What changes, and what deliberately does not:

* Legitimate transactions shift. Their ``amount`` scales by
  ``1 + drift_shift`` and features v1-v6 drift toward the fraud cluster.
  This models a real, mundane cause — a new payment processor, a
  seasonal change in spending — rather than corruption.
* Fraudulent transactions do not change at all.
* Features v7-v28 do not change at all.

That asymmetry is what makes the experiment interpretable. A boundary
fit before the shift sees a rising false-positive rate; a boundary refit
after it recovers, because fraud and (shifted) legit are still
separable. If everything moved at once there would be no way to tell a
recoverable covariate shift from a broken feed, and "retrain on it" would
be the wrong response to half the cases.

Every parameter is recorded on the DatasetVersion — see
``fraud_data.drift_parameters`` — so the window can be regenerated from
the audit trail alone.
"""

from __future__ import annotations

from case_studies.fraud_detection import data as fraud_data
from demo.context import DemoContext
from demo.reporting import bullet, detail, kv, section
from demo.steps import _monitoring


def run(ctx: DemoContext) -> None:
    """Generate and register the drifted production window."""
    cfg = ctx.config

    section("Controlled noise injection")
    params = fraud_data.drift_parameters(
        drift_shift=cfg.drifted_drift_shift,
        seed=cfg.drifted_window_seed,
        n_rows=cfg.window_rows,
        fraud_ratio=cfg.fraud_ratio,
    )
    kv("Noise type", params["noise_type"], width=22)
    kv("Random seed", params["random_seed"], width=22)
    kv("drift_shift", params["parameters"]["drift_shift"], width=22)
    kv("Legit amount scale", f"x{params['parameters']['legit_amount_scale']}", width=22)
    kv(
        "Legit V-feature shift",
        f"mean +{params['parameters']['legit_v_feature_mean_shift']}, "
        f"std {params['parameters']['legit_v_feature_shift_std']}",
        width=22,
    )
    kv("Affected features", ", ".join(params["affected_features"]), width=22)
    kv("Unaffected features", f"{len(params['unaffected_features'])} features", width=22)
    kv("Affected records", f"{params['affected_records']:,} of {params['n_rows']:,}", width=22)
    kv("Generator", params["generator"], width=22)

    detail("")
    detail("Held constant on purpose:")
    bullet("fraud rows are generated identically at every drift_shift")
    bullet(f"{len(params['unaffected_features'])} V-features are untouched")
    detail("")

    version_id, path = _monitoring.register_window(
        ctx,
        filename=cfg.drifted_window_filename,
        seed=cfg.drifted_window_seed,
        drift_shift=cfg.drifted_drift_shift,
        label="drifted",
    )
    ctx.drifted_window_version_id = version_id

    detail("")
    detail("Shifted window written and registered. Feeding it into the same")
    detail("monitoring pipeline the baseline window went through.")

    ctx.record(
        "drift-injection",
        "NOISE_INJECTED",
        window="drifted",
        noise_type=params["noise_type"],
        seed=params["random_seed"],
        drift_shift=params["parameters"]["drift_shift"],
        affected_features=len(params["affected_features"]),
        affected_records=params["affected_records"],
        dataset_version_id=version_id,
    )
