"""Model promotion policy (Week 3, Day 18).

A training run succeeding does not mean a model should be promoted to
PRODUCTION. The framework therefore applies an explicit promotion
policy that evaluates:

    1. Minimum metric thresholds (e.g. f1 >= 0.85).
    2. Whether the new model beats the existing production model on
       every tracked metric (``must_beat_production``).
    3. Optional absolute upper bounds (e.g. no metric above 1.0 — the
       framework does not enforce this; users can add it).

The policy is intentionally small and explicit. Every check produces
a reason; the decision is explainable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)


# ---------------------------------------------------------------------- #
# Configuration & decision
# ---------------------------------------------------------------------- #


@dataclass
class PromotionConfig:
    """Configuration for the promotion policy."""

    min_metrics: dict[str, float] = field(default_factory=dict)
    # If True, the candidate must beat the production model on every
    # metric they share.
    must_beat_production: bool = True
    # If True, the policy allows a *first* candidate to be promoted
    # even when no production model exists. Defaults to True because
    # the first promotion is typically the cold-start case.
    allow_cold_start: bool = True
    # If provided, the candidate is rejected when any of its metrics
    # fall *below* these "never go below" floors.
    min_floors: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PromotionConfig":
        if data is None:
            return cls()
        return cls(
            min_metrics=dict(data.get("min_metrics", {}) or {}),
            must_beat_production=bool(
                data.get("must_beat_production", True)
            ),
            allow_cold_start=bool(data.get("allow_cold_start", True)),
            min_floors=dict(data.get("min_floors", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_metrics": dict(self.min_metrics),
            "must_beat_production": self.must_beat_production,
            "allow_cold_start": self.allow_cold_start,
            "min_floors": dict(self.min_floors),
        }


@dataclass
class PromotionContext:
    """Inputs to the promotion decision."""

    candidate: ModelVersion
    production: Optional[ModelVersion] = None


@dataclass
class PromotionDecision:
    """Explainable promotion decision."""

    approved: bool
    reasons: list[str]
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reasons": list(self.reasons),
            "details": dict(self.details),
        }


# ---------------------------------------------------------------------- #
# Policy
# ---------------------------------------------------------------------- #


class ModelPromotionPolicy:
    """Reusable, explainable promotion policy.

    The policy is stateless. It does not mutate the database; the
    caller (e.g. the :class:`ModelManager` or a workflow) is
    responsible for actually changing the model state.
    """

    def evaluate(
        self,
        context: PromotionContext,
        config: PromotionConfig | dict[str, Any] | None = None,
    ) -> PromotionDecision:
        cfg = (
            config
            if isinstance(config, PromotionConfig)
            else PromotionConfig.from_dict(config)
        )

        reasons: list[str] = []
        details: dict[str, Any] = {
            "candidate_metrics": _safe_metrics(context.candidate),
        }
        candidate_metrics = details["candidate_metrics"]

        # 1. Absolute minimum thresholds
        for metric, threshold in cfg.min_metrics.items():
            value = candidate_metrics.get(metric)
            if value is None:
                reasons.append(
                    f"Candidate has no value for required metric "
                    f"{metric!r}"
                )
                continue
            if value < threshold:
                reasons.append(
                    f"{metric} {value:.4f} is below minimum threshold "
                    f"{threshold:.4f}"
                )

        # 2. Floors (candidate must NOT go below these)
        for metric, floor in cfg.min_floors.items():
            value = candidate_metrics.get(metric)
            if value is not None and value < floor:
                reasons.append(
                    f"{metric} {value:.4f} is below floor {floor:.4f}"
                )

        # 3. Comparison with the production model
        if cfg.must_beat_production:
            if context.production is None:
                if not cfg.allow_cold_start:
                    reasons.append(
                        "No production model to compare against and "
                        "policy does not allow cold start"
                    )
            else:
                prod_metrics = _safe_metrics(context.production)
                details["production_metrics"] = prod_metrics
                shared = set(prod_metrics) & set(candidate_metrics)
                for metric in sorted(shared):
                    p, c = prod_metrics[metric], candidate_metrics[metric]
                    if c <= p:
                        reasons.append(
                            f"{metric} did not beat production: "
                            f"candidate={c:.4f}, production={p:.4f}"
                        )

        approved = len(reasons) == 0
        return PromotionDecision(
            approved=approved,
            reasons=reasons,
            details=details,
        )


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _safe_metrics(mv: ModelVersion) -> dict[str, float]:
    if not mv.metrics_json:
        return {}
    try:
        raw = json.loads(mv.metrics_json)
    except (ValueError, TypeError):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out
