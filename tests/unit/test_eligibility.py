"""Unit tests for the training eligibility policy."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import DriftEvaluation
from mlops_framework.database.models.model import Model
from mlops_framework.database.models.model_promotion_event import (
    ModelPromotionEvent,
)
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessEvaluation,
    ReadinessStatus,
)
from mlops_framework.database.models.serving_instance import ServingInstance
from mlops_framework.database.models.training_run import (
    RunStatus,
    TrainingRun,
    TriggerType,
)
from mlops_framework.drift.detector import DriftResult, FeatureDrift
from mlops_framework.governance.eligibility import (
    EligibilityConfig,
    EligibilityContext,
    TrainingEligibilityPolicy,
)
from mlops_framework.readiness.engine import (
    ReadinessCheck,
    ReadinessCheckOutcome,
    ReadinessEngine,
    ReadinessResult,
    TrainingPolicy,
)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _ready_result(version_id: int) -> ReadinessResult:
    return ReadinessResult(
        status=ReadinessStatus.READY,
        passed=True,
        checks=[ReadinessCheck("size", ReadinessCheckOutcome.PASSED)],
        reasons=[],
        policy=TrainingPolicy(),
        dataset_version_id=version_id,
        evaluated_at=datetime.now(timezone.utc),
        observed_row_count=1000,
    )


def _blocked_result(version_id: int) -> ReadinessResult:
    return ReadinessResult(
        status=ReadinessStatus.BLOCKED,
        passed=False,
        checks=[ReadinessCheck("size", ReadinessCheckOutcome.FAILED, "too small")],
        reasons=["Dataset contains fewer rows than required"],
        policy=TrainingPolicy(),
        dataset_version_id=version_id,
        evaluated_at=datetime.now(timezone.utc),
        observed_row_count=10,
    )


def _drift(detected: bool) -> DriftResult:
    return DriftResult(
        drift_detected=detected,
        score=0.1 if detected else 0.0,
        method="ks",
        threshold=0.05,
        feature_results=[
            FeatureDrift(
                "x", "ks", 0.1 if detected else 0.0, detected, 0.001 if detected else 0.5
            )
        ],
    )


class TestReadinessGate:
    def test_blocked_dataset_is_not_eligible(self, session):
        ctx = EligibilityContext(readiness=_blocked_result(1))
        decision = TrainingEligibilityPolicy(session).evaluate(ctx)
        assert decision.eligible is False
        assert any("not READY" in r for r in decision.reasons)

    def test_ready_dataset_passes_when_no_other_rules(self, session):
        ctx = EligibilityContext(readiness=_ready_result(1))
        decision = TrainingEligibilityPolicy(session).evaluate(ctx)
        assert decision.eligible is True

    def test_missing_readiness_is_not_eligible(self, session):
        ctx = EligibilityContext(readiness=None)
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(require_ready=True)
        )
        assert decision.eligible is False
        assert any("No readiness" in r for r in decision.reasons)

    def test_require_ready_false_skips_readiness(self, session):
        ctx = EligibilityContext(readiness=None)
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(require_ready=False)
        )
        assert decision.eligible is True


class TestCooldown:
    def test_cooldown_active(self, session):
        run = TrainingRun(
            dataset_version_id=1,
            status=RunStatus.SUCCESS,
            trigger_type=TriggerType.MANUAL,
            started_at=datetime.now(timezone.utc) - timedelta(hours=2),
            completed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            last_training_run=run,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(cooldown_hours=24)
        )
        assert decision.eligible is False
        assert any("cooldown" in r for r in decision.reasons)

    def test_cooldown_passed(self, session):
        run = TrainingRun(
            dataset_version_id=1,
            status=RunStatus.SUCCESS,
            trigger_type=TriggerType.MANUAL,
            completed_at=datetime.now(timezone.utc) - timedelta(hours=72),
        )
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            last_training_run=run,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(cooldown_hours=24)
        )
        assert decision.eligible is True


class TestDriftGates:
    def test_drift_required_blocks_when_no_drift(self, session):
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            drift=_drift(False),
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(require_drift_to_retrain=True)
        )
        assert decision.eligible is False
        assert any("drift" in r.lower() for r in decision.reasons)

    def test_drift_required_passes_when_drift(self, session):
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            drift=_drift(True),
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(require_drift_to_retrain=True)
        )
        assert decision.eligible is True

    def test_drift_blocked_when_drift_detected(self, session):
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            drift=_drift(True),
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(block_when_drift_detected=True)
        )
        assert decision.eligible is False


class TestMinNewRows:
    def test_min_new_rows_blocks(self, session):
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            candidate_row_count=2000,
            production_row_count=1900,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(min_new_rows=500)
        )
        assert decision.eligible is False

    def test_min_new_rows_passes(self, session):
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            candidate_row_count=2000,
            production_row_count=100,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(min_new_rows=500)
        )
        assert decision.eligible is True


class TestForce:
    def test_force_short_circuits_to_eligible(self, session):
        ctx = EligibilityContext(
            readiness=_blocked_result(1), force=True
        )
        decision = TrainingEligibilityPolicy(session).evaluate(ctx)
        assert decision.eligible is True
        assert any("forced" in r for r in decision.reasons)


class TestProductionExistence:
    def test_require_production_blocks_when_none(self, session):
        ctx = EligibilityContext(readiness=_ready_result(1))
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(require_existing_production=True)
        )
        assert decision.eligible is False

    def test_block_when_production_blocks_if_exists(self, session):
        mv = ModelVersion(
            model_id=1,
            dataset_version_id=1,
            version_number=1,
            state=ModelState.PRODUCTION,
            metrics_json=json.dumps({"f1": 0.9}),
        )
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            production_model_version=mv,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx, EligibilityConfig(block_when_production_exists=True)
        )
        assert decision.eligible is False


class TestProductionMetrics:
    def test_block_when_production_meets_thresholds(self, session):
        mv = ModelVersion(
            model_id=1,
            dataset_version_id=1,
            version_number=1,
            state=ModelState.PRODUCTION,
            metrics_json=json.dumps({"f1": 0.95, "auprc": 0.9}),
        )
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            production_model_version=mv,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx,
            EligibilityConfig(
                block_when_production_metrics_meet={"f1": 0.85, "auprc": 0.8}
            ),
        )
        assert decision.eligible is False
        assert any("already meets" in r for r in decision.reasons)

    def test_require_production_below_blocks_when_met(self, session):
        mv = ModelVersion(
            model_id=1,
            dataset_version_id=1,
            version_number=1,
            state=ModelState.PRODUCTION,
            metrics_json=json.dumps({"f1": 0.95}),
        )
        ctx = EligibilityContext(
            readiness=_ready_result(1),
            production_model_version=mv,
        )
        decision = TrainingEligibilityPolicy(session).evaluate(
            ctx,
            EligibilityConfig(
                require_production_below={"f1": 0.9}
            ),
        )
        # production f1 (0.95) is not below 0.9 — block
        assert decision.eligible is False


class TestFromDict:
    def test_from_dict_parses(self):
        cfg = EligibilityConfig.from_dict(
            {
                "require_ready": True,
                "cooldown_hours": 12,
                "min_new_rows": 100,
                "block_when_production_metrics_meet": {"f1": 0.85},
            }
        )
        assert cfg.cooldown_hours == 12
        assert cfg.min_new_rows == 100
        assert cfg.block_when_production_metrics_meet == {"f1": 0.85}
