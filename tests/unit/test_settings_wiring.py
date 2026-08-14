"""Unit tests for CP4's default-resolution wiring.

Four call sites — ``ModelPromotionPolicy.evaluate``,
``TrainingEligibilityPolicy.evaluate``, ``ReadinessEngine.evaluate``,
``DriftService.evaluate`` — now resolve their own default from
persisted Settings (``FrameworkSettingsManager``) when the caller
evaluates with ``config``/``policy=None``, instead of always falling
back to the dataclass's own bare default. See
``tests/integration/test_governance_end_to_end.py``'s
``TestSettingsDefaultResolution`` for the same behaviour exercised
through a real, fully-wired ``RetrainingWorkflow`` run instead of one
policy class at a time.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.drift.detector import DriftConfig, DriftService, ScipyDriftDetector
from mlops_framework.framework_settings.manager import (
    DRIFT,
    ELIGIBILITY,
    PROMOTION,
    TRAINING_POLICY,
    FrameworkSettingsManager,
)
from mlops_framework.governance.eligibility import (
    EligibilityContext,
    TrainingEligibilityPolicy,
)
from mlops_framework.governance.promotion import (
    ModelPromotionPolicy,
    PromotionConfig,
    PromotionContext,
)
from mlops_framework.readiness.engine import ReadinessEngine


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _mv(metrics: dict[str, float]) -> ModelVersion:
    return ModelVersion(
        model_id=1, dataset_version_id=1, version_number=1,
        state=ModelState.CANDIDATE, metrics_json=json.dumps(metrics),
    )


def _dv(session, *, name: str = "ds", row_count: int = 1000) -> DatasetVersion:
    ds = Dataset(name=name)
    session.add(ds)
    session.flush()
    v = DatasetVersion(
        dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
        checksum="0" * 64, schema_hash="0" * 64, row_count=row_count,
    )
    session.add(v)
    session.flush()
    return v


class TestModelPromotionPolicy:
    def test_no_session_ignores_settings_entirely(self, session):
        """Backward compat: a bare ModelPromotionPolicy() (the shape
        every pre-CP4 caller and test uses) never looks at Settings,
        even when something's been persisted."""
        FrameworkSettingsManager(session).set_raw(PROMOTION, {"min_floors": {"f1": 0.99}})
        decision = ModelPromotionPolicy().evaluate(
            PromotionContext(candidate=_mv({"f1": 0.5}))
        )
        assert decision.approved is True

    def test_with_session_resolves_persisted_default(self, session):
        FrameworkSettingsManager(session).set_raw(PROMOTION, {"min_floors": {"f1": 0.99}})
        decision = ModelPromotionPolicy(session=session).evaluate(
            PromotionContext(candidate=_mv({"f1": 0.5}))
        )
        assert decision.approved is False
        assert any("below floor" in r for r in decision.reasons)

    def test_with_session_but_empty_settings_matches_bare_default(self, session):
        decision = ModelPromotionPolicy(session=session).evaluate(
            PromotionContext(candidate=_mv({"f1": 0.01}))
        )
        assert decision.approved is True  # cold start, no floors/metrics configured

    def test_explicit_config_still_wins_over_settings(self, session):
        FrameworkSettingsManager(session).set_raw(PROMOTION, {"min_floors": {"f1": 0.99}})
        decision = ModelPromotionPolicy(session=session).evaluate(
            PromotionContext(candidate=_mv({"f1": 0.5})), config=PromotionConfig()
        )
        assert decision.approved is True


class TestTrainingEligibilityPolicy:
    def test_empty_settings_matches_bare_default(self, session):
        """Bare EligibilityConfig() defaults require_ready=True; with no
        readiness result in the context, that's the same "no readiness
        evaluation provided" rejection a hardcoded EligibilityConfig()
        already produced pre-CP4 — proves the empty-settings path
        resolves to the identical dataclass default, not a different
        (accidentally more permissive) one."""
        policy = TrainingEligibilityPolicy(session)
        decision = policy.evaluate(EligibilityContext(readiness=None))
        assert decision.eligible is False
        assert "No readiness evaluation provided" in decision.reasons

    def test_persisted_require_ready_false_skips_the_readiness_gate(self, session):
        FrameworkSettingsManager(session).set_raw(ELIGIBILITY, {"require_ready": False})
        policy = TrainingEligibilityPolicy(session)
        # readiness=None would normally fail the require_ready gate.
        decision = policy.evaluate(EligibilityContext(readiness=None))
        assert decision.eligible is True

    def test_explicit_config_still_wins_over_settings(self, session):
        FrameworkSettingsManager(session).set_raw(ELIGIBILITY, {"require_ready": False})
        from mlops_framework.governance.eligibility import EligibilityConfig

        decision = TrainingEligibilityPolicy(session).evaluate(
            EligibilityContext(readiness=None), config=EligibilityConfig()
        )
        assert decision.eligible is False  # bare EligibilityConfig() has require_ready=True


class TestReadinessEngine:
    def test_empty_settings_matches_bare_default(self, session):
        version = _dv(session, row_count=1)
        result = ReadinessEngine(session).evaluate(version)
        assert result.is_ready is True  # required_size=0 by default

    def test_persisted_required_size_blocks(self, session):
        FrameworkSettingsManager(session).set_raw(TRAINING_POLICY, {"required_size": 999_999})
        version = _dv(session, row_count=1000)
        result = ReadinessEngine(session).evaluate(version)
        assert result.is_ready is False

    def test_explicit_policy_still_wins_over_settings(self, session):
        FrameworkSettingsManager(session).set_raw(TRAINING_POLICY, {"required_size": 999_999})
        from mlops_framework.readiness.engine import TrainingPolicy

        version = _dv(session, row_count=1000)
        result = ReadinessEngine(session).evaluate(version, policy=TrainingPolicy())
        assert result.is_ready is True


class TestDriftService:
    def _feature_data(self, n: int = 40) -> dict[str, list[float]]:
        return {"x": [float(i) for i in range(n)]}

    def test_empty_settings_matches_bare_default(self, session):
        ref = _dv(session, name="ref", row_count=1000)
        cur = _dv(session, name="cur", row_count=1000)
        service = DriftService(session, ScipyDriftDetector())
        data = self._feature_data()
        result = service.evaluate(
            reference_version=ref, current_version=cur,
            reference_data=data, current_data=data,
        )
        assert result.threshold == DriftConfig().threshold

    def test_persisted_threshold_is_used(self, session):
        FrameworkSettingsManager(session).set_raw(DRIFT, {"threshold": 0.9, "min_samples": 5})
        ref = _dv(session, name="ref", row_count=1000)
        cur = _dv(session, name="cur", row_count=1000)
        service = DriftService(session, ScipyDriftDetector())
        data = self._feature_data()
        result = service.evaluate(
            reference_version=ref, current_version=cur,
            reference_data=data, current_data=data,
        )
        assert result.threshold == 0.9

    def test_explicit_config_still_wins_over_settings(self, session):
        FrameworkSettingsManager(session).set_raw(DRIFT, {"threshold": 0.9})
        ref = _dv(session, name="ref", row_count=1000)
        cur = _dv(session, name="cur", row_count=1000)
        service = DriftService(session, ScipyDriftDetector())
        data = self._feature_data()
        result = service.evaluate(
            reference_version=ref, current_version=cur,
            reference_data=data, current_data=data,
            config=DriftConfig(threshold=0.05),
        )
        assert result.threshold == 0.05
