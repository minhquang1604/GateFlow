"""Unit tests for the model promotion policy."""

from __future__ import annotations

import json

from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.governance.promotion import (
    ModelPromotionPolicy,
    PromotionConfig,
    PromotionContext,
)


def _mv(metrics: dict[str, float], state: ModelState = ModelState.CANDIDATE) -> ModelVersion:
    return ModelVersion(
        model_id=1,
        dataset_version_id=1,
        version_number=1,
        state=state,
        metrics_json=json.dumps(metrics),
    )


class TestMinMetrics:
    def test_candidate_meets_thresholds(self):
        candidate = _mv({"f1": 0.9, "auprc": 0.85})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate),
            PromotionConfig(min_metrics={"f1": 0.85, "auprc": 0.8}),
        )
        assert decision.approved is True

    def test_candidate_below_threshold(self):
        candidate = _mv({"f1": 0.81})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate),
            PromotionConfig(min_metrics={"f1": 0.85}),
        )
        assert decision.approved is False
        assert any("f1" in r and "below" in r for r in decision.reasons)

    def test_missing_required_metric(self):
        candidate = _mv({"f1": 0.9})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate),
            PromotionConfig(min_metrics={"auprc": 0.8}),
        )
        assert decision.approved is False
        assert any("auprc" in r for r in decision.reasons)


class TestProductionComparison:
    def test_must_beat_production_passes(self):
        candidate = _mv({"f1": 0.92, "auprc": 0.88})
        production = _mv(
            {"f1": 0.9, "auprc": 0.85}, state=ModelState.PRODUCTION
        )
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate, production=production),
            PromotionConfig(
                min_metrics={"f1": 0.85, "auprc": 0.8},
                must_beat_production=True,
            ),
        )
        assert decision.approved is True

    def test_must_beat_production_fails_when_worse(self):
        candidate = _mv({"f1": 0.88, "auprc": 0.85})
        production = _mv(
            {"f1": 0.9, "auprc": 0.87}, state=ModelState.PRODUCTION
        )
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate, production=production),
            PromotionConfig(
                min_metrics={"f1": 0.85, "auprc": 0.8},
                must_beat_production=True,
            ),
        )
        assert decision.approved is False
        assert any("did not beat" in r for r in decision.reasons)

    def test_cold_start_allowed_by_default(self):
        candidate = _mv({"f1": 0.9})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate, production=None),
            PromotionConfig(
                min_metrics={"f1": 0.85},
                must_beat_production=True,
                allow_cold_start=True,
            ),
        )
        assert decision.approved is True

    def test_cold_start_blocked_when_disallowed(self):
        candidate = _mv({"f1": 0.9})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate, production=None),
            PromotionConfig(
                min_metrics={"f1": 0.85},
                must_beat_production=True,
                allow_cold_start=False,
            ),
        )
        assert decision.approved is False
        assert any("cold start" in r for r in decision.reasons)


class TestMinFloors:
    def test_floor_violation_blocks(self):
        candidate = _mv({"loss": 0.1})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate),
            PromotionConfig(min_floors={"f1": 0.5, "loss": 0.05}),
        )
        # f1 is missing so not in floor check; loss=0.1 vs floor=0.05 passes
        # so we need a real floor violation.
        candidate = _mv({"loss": 0.01})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate),
            PromotionConfig(min_floors={"loss": 0.05}),
        )
        assert decision.approved is False
        assert any("floor" in r for r in decision.reasons)


class TestToDict:
    def test_decision_serializable(self):
        candidate = _mv({"f1": 0.9})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=candidate),
            PromotionConfig(min_metrics={"f1": 0.85}),
        )
        json.dumps(decision.to_dict())
