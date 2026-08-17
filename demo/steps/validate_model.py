"""Phase 11 — the evidence behind the promotion decision.

This step does **not** decide anything. The decision was already made,
by ``ModelPromotionPolicy`` inside the retraining workflow, before V2
was allowed anywhere near production. What happens here is that the
numbers that decision rested on are laid out so a reader can check it.

Three measurements, deliberately kept apart:

* **V1 stored** — what V1 scored at training time, on the population
  that existed then.
* **V1 live** — V1 re-scored *now*, on the drifted window. This is the
  number that shows the model degrading, and it is the honest statement
  of the problem.
* **V2** — the candidate, trained on V1 + the drifted window.

Reporting only the first and last would let a retrain be justified by
comparing two numbers measured on two different populations. The middle
column is what makes the comparison mean anything — and it is also why
the promotion policy uses an absolute floor rather than
``must_beat_production``: V1's stored metric is not a fair bar once the
population it was measured on is gone.
"""

from __future__ import annotations

import json
from typing import Any

from case_studies.fraud_detection import data as fraud_data
from demo.context import DemoContext
from demo.reporting import banner, detail, kv, metric_comparison, section
from mlops_framework.database.models.model_version import ModelVersion


def run(ctx: DemoContext, outcome: Any) -> dict[str, Any]:
    """Report the validation evidence. Returns the three metric sets."""
    cfg = ctx.config

    banner("MODEL VALIDATION")

    v1_stored = _stored_metrics(ctx, ctx.v1_model_version_id)
    v2_metrics = _stored_metrics(ctx, outcome.model_version_id)
    v1_live = _score_live(ctx)

    metric_comparison(
        v1_stored=v1_stored,
        v1_live=v1_live,
        v2_metrics=v2_metrics,
        thresholds=dict(cfg.promotion_min_metrics),
    )

    section("Interpretation")
    if v1_live and v1_stored.get("f1") is not None:
        drop = v1_stored["f1"] - v1_live.get("f1", 0.0)
        detail(
            f"V1 lost {drop:.4f} F1 between the population it was trained "
            f"on and the drifted window"
        )
    if v1_live and v2_metrics.get("f1") is not None:
        gain = v2_metrics["f1"] - v1_live.get("f1", 0.0)
        detail(
            f"V2 scores {gain:+.4f} F1 against V1's live performance on "
            f"the same drifted data"
        )

    section("Acceptance criteria")
    kv("Policy", "absolute floor (must_beat_production=False)", width=22)
    for metric, floor in cfg.promotion_min_metrics.items():
        actual = v2_metrics.get(metric)
        verdict = (
            "PASS" if actual is not None and float(actual) >= floor else "FAIL"
        )
        kv(f"{metric} >= {floor}", f"{_fmt(actual)}  [{verdict}]", width=22)

    decision_step = next(
        (s for s in outcome.steps if s.name == "promotion"), None
    )
    print()
    if outcome.promoted:
        kv("Validation", "PASSED", width=22)
        detail("V2 registered, V1 archived, V2 promoted to PRODUCTION.")
    else:
        kv("Validation", "FAILED", width=22)
        if decision_step is not None:
            detail(f"Reason: {decision_step.detail}")
        detail("V2 was NOT promoted. V1 remains the production model.")

    ctx.record(
        "model-validation",
        "VALIDATION_PASSED" if outcome.promoted else "VALIDATION_FAILED",
        v1_stored_f1=v1_stored.get("f1"),
        v1_live_f1=v1_live.get("f1"),
        v2_f1=v2_metrics.get("f1"),
        status="PASSED" if outcome.promoted else "FAILED",
    )
    return {"v1_stored": v1_stored, "v1_live": v1_live, "v2": v2_metrics}


# ---------------------------------------------------------------------- #


def _stored_metrics(ctx: DemoContext, model_version_id: int | None) -> dict[str, Any]:
    if model_version_id is None:
        return {}
    with ctx.db.get_session() as session:
        mv = session.get(ModelVersion, model_version_id)
        if mv is None or not mv.metrics_json:
            return {}
        try:
            return json.loads(mv.metrics_json)
        except (ValueError, TypeError):
            return {}


def _score_live(ctx: DemoContext) -> dict[str, Any]:
    """Load V1's actual artifact and score it on the drifted window.

    Best-effort: if the artifact cannot be fetched, the column is simply
    absent rather than fabricated. A missing measurement is a gap in the
    evidence; an invented one is a false claim, and this is the number
    the whole retrain argument rests on.
    """
    if not ctx.v1_mlflow_run_id:
        detail("V1 has no MLflow run id — skipping the live re-score.")
        return {}
    try:
        import mlflow
        import numpy as np
        import pandas as pd
        import xgboost as xgb
        from sklearn.metrics import (
            f1_score,
            precision_score,
            recall_score,
            roc_auc_score,
        )

        path = mlflow.artifacts.download_artifacts(
            run_id=ctx.v1_mlflow_run_id, artifact_path="model.json"
        )
        model = xgb.XGBClassifier()
        model.load_model(path)

        df = fraud_data.normalize_columns(
            pd.read_csv(ctx.config.local_path(ctx.config.drifted_window_filename))
        )
        X = df[fraud_data.feature_columns()].to_numpy(dtype=np.float32)
        y = df[fraud_data.target_column()].to_numpy(dtype=np.int32)
        pred = model.predict(X)

        metrics = {
            "f1": float(f1_score(y, pred, zero_division=0)),
            "precision": float(precision_score(y, pred, zero_division=0)),
            "recall": float(recall_score(y, pred, zero_division=0)),
        }
        # Only meaningful when both classes are present in the window.
        if len(set(y.tolist())) > 1:
            proba = model.predict_proba(X)[:, 1]
            metrics["roc_auc"] = float(roc_auc_score(y, proba))
        return metrics
    except Exception as exc:
        detail(f"Could not re-score V1 on the drifted window: {exc}")
        return {}


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)
