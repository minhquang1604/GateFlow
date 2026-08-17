"""Phase 3 — production traffic that has NOT drifted.

The baseline. A window drawn from the same population V1 trained on,
scored by the same detector against the same threshold, expected to come
back NORMAL.

Skipping this step would make the demo strictly weaker: a detector that
has only ever been shown data engineered to trip it has not been shown
to discriminate. This is the negative control.
"""

from __future__ import annotations

from demo.context import DemoContext
from demo.reporting import detail, section
from demo.steps import _monitoring


def run(ctx: DemoContext) -> bool:
    """Score a normal window. Returns whether drift was (wrongly) flagged."""
    cfg = ctx.config
    ctx.state.monitoring_status = "ACTIVE"

    section("Production traffic — baseline window")
    detail(
        "Same population as the training reference; only the sampling "
        "seed differs, so these are fresh transactions rather than a "
        "replay of the training rows."
    )
    version_id, path = _monitoring.register_window(
        ctx,
        filename=cfg.normal_window_filename,
        seed=cfg.normal_window_seed,
        drift_shift=cfg.normal_drift_shift,
        label="baseline",
    )
    ctx.normal_window_version_id = version_id

    result = _monitoring.monitor(
        ctx,
        window_version_id=version_id,
        window_path=path,
        window_label="baseline window",
        notes="Phase 3 — baseline production window (negative control)",
    )

    if result.drift_detected:
        # Not fatal — but say so plainly rather than letting the demo
        # sail past a result that undercuts everything after it.
        ctx.state.drift_status = "UNEXPECTED_DRIFT"
        detail(
            "UNEXPECTED: the baseline window was flagged. The later "
            "detection is not evidence of a targeted shift until this "
            "is explained."
        )
    else:
        ctx.state.drift_status = "NORMAL"
        detail("Baseline confirmed — the detector does not flag the reference population.")
        detail("System continues monitoring.")

    ctx.record(
        "drift-monitor",
        "WINDOW_EVALUATED",
        window="baseline",
        drift_detected=result.drift_detected,
        score=round(result.score, 4),
        threshold=result.threshold,
        status="NORMAL" if not result.drift_detected else "UNEXPECTED_DRIFT",
    )
    return result.drift_detected
