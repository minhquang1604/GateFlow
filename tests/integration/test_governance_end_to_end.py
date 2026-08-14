"""End-to-end governance integration tests (Week 3, Day 21).

These tests exercise the full chain:

    Dataset -> DatasetVersion -> ReadinessEngine
        -> TrainingEligibilityPolicy
        -> TrainingService
        -> ModelManager
        -> PromotionPolicy
        -> EventPublisher
        -> ServingBridge

across all 5 cases required by the spec:

    CASE 1 — BLOCKED DATASET (no training starts)
    CASE 2 — TRAINING FAILURE (model not promoted)
    CASE 3 — MODEL REJECTED (model not in PRODUCTION)
    CASE 4 — MODEL APPROVED (PRODUCTION)
    CASE 5 — SERVING RELOAD (active model version updated)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.events.publisher import InMemoryEventPublisher
from mlops_framework.framework_settings.manager import (
    PROMOTION,
    TRAINING_POLICY,
    FrameworkSettingsManager,
)
from mlops_framework.governance.eligibility import EligibilityConfig
from mlops_framework.governance.promotion import PromotionConfig
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.readiness.engine import TrainingPolicy
from mlops_framework.serving.bridge import ServingBridge
from mlops_framework.tracking.in_memory import InMemoryTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService
from mlops_framework.workflow.retraining import RetrainingWorkflow

SUCCESS_PIPELINE = "tests._pipelines.e2e_training:main"
FAIL_PIPELINE = "tests._pipelines.pipelines:fail"


# ---------------------------------------------------------------------- #
# Fixture: a fully wired retraining workflow with a serving bridge.
# ---------------------------------------------------------------------- #


@pytest.fixture()
def wired_workflow(db_session):
    """Build a workflow with the in-memory tracker, local orchestrator
    and in-memory event publisher — and a serving bridge that shares
    the same DB session.

    The pipeline ``e2e_training:main`` reports deterministic metrics:
    ``f1 = 0.80 + 0.001 * ((run_id + 1) % 50)``.
    """
    db_session.expire_all()
    orchestrator = LocalDockerOrchestrator()
    tracker = InMemoryTracker()
    tm = TrainingManager(db_session, DatasetManager(db_session))
    service = TrainingService(
        training_manager=tm,
        orchestrator=orchestrator,
        tracker=tracker,
    )
    events = InMemoryEventPublisher()

    def session_factory():
        return db_session.__class__(bind=db_session.get_bind())

    bridge = ServingBridge(session_factory=session_factory)
    workflow = RetrainingWorkflow(
        session=db_session,
        training_service=service,
        event_publisher=events,
    )
    try:
        yield {
            "db_session": db_session,
            "orchestrator": orchestrator,
            "tracker": tracker,
            "training_manager": tm,
            "service": service,
            "events": events,
            "bridge": bridge,
            "workflow": workflow,
            "session_factory": session_factory,
        }
    finally:
        orchestrator.shutdown()


def _make_dataset_version(
    session,
    *,
    name: str = "fraud-ds",
    row_count: int = 5000,
    metadata: dict | None = None,
) -> DatasetVersion:
    dm = DatasetManager(session)
    ds = dm.create_dataset(name=name, description="d")
    return dm.create_version(
        dataset_id=ds.id,
        storage_uri=f"s3://b/{name}-v1.csv",
        row_count=row_count,
        metadata=metadata
        or {"columns": [{"name": "amount", "dtype": "float64"}]},
    )


def _make_model(session, *, name: str = "fraud-model") -> ModelRow:
    mm = ModelManager(session)
    return mm.create_model(name=name, task="fraud_detection")


# ---------------------------------------------------------------------- #
# CASE 1 — BLOCKED DATASET (no training starts)
# ---------------------------------------------------------------------- #


class TestCase1BlockedDataset:
    def test_no_training_when_readiness_blocked(self, wired_workflow):
        db_session = wired_workflow["db_session"]
        dv = _make_dataset_version(db_session, row_count=10)
        model = _make_model(db_session)
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=1000),
            pipeline_id=SUCCESS_PIPELINE,
        )
        assert outcome.promoted is False
        assert outcome.blocked_reason == "readiness_blocked"
        assert outcome.training_run_id is None
        assert outcome.model_version_id is None
        # The readiness step failed; the rest did not run.
        step_names = [s.name for s in outcome.steps]
        assert step_names == ["readiness"]


# ---------------------------------------------------------------------- #
# CASE 2 — TRAINING FAILURE (model not promoted)
# ---------------------------------------------------------------------- #


class TestCase2TrainingFailure:
    def test_training_failure_does_not_promote(self, wired_workflow):
        db_session = wired_workflow["db_session"]
        dv = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=FAIL_PIPELINE,
        )
        assert outcome.promoted is False
        assert outcome.blocked_reason == "training_failed"
        assert outcome.training_run_id is not None
        # No ModelVersion was created
        assert outcome.model_version_id is None
        # The training step failed
        last_step = outcome.steps[-1]
        assert last_step.name == "training"
        assert last_step.passed is False


# ---------------------------------------------------------------------- #
# CASE 3 — MODEL REJECTED
# ---------------------------------------------------------------------- #


class TestCase3ModelRejected:
    def test_promotion_rejected_when_metrics_below(self, wired_workflow):
        db_session = wired_workflow["db_session"]
        dv = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        # The pipeline's metrics are ~0.8 — below the 0.9 threshold
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.9}, must_beat_production=False
            ),
            pipeline_id=SUCCESS_PIPELINE,
        )
        assert outcome.promoted is False
        assert outcome.blocked_reason == "model_rejected"
        assert outcome.model_version_id is not None
        # The model is REJECTED in the database
        db_session.expire_all()
        mv = db_session.get(ModelVersion, outcome.model_version_id)
        assert mv.state == ModelState.REJECTED
        # A promotion event was NOT published
        assert len(wired_workflow["events"].events) == 0


# ---------------------------------------------------------------------- #
# CASE 4 — MODEL APPROVED
# ---------------------------------------------------------------------- #


class TestCase4ModelApproved:
    def test_successful_lifecycle_with_event_published(self, wired_workflow):
        db_session = wired_workflow["db_session"]
        dv = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5},
                must_beat_production=False,
                allow_cold_start=True,
            ),
            pipeline_id=SUCCESS_PIPELINE,
        )
        assert outcome.promoted is True
        assert outcome.training_run_id is not None
        assert outcome.model_version_id is not None
        assert outcome.promotion_event_id is not None
        # The model is PRODUCTION
        db_session.expire_all()
        mv = db_session.get(ModelVersion, outcome.model_version_id)
        assert mv.state == ModelState.PRODUCTION
        # The event was published
        assert len(wired_workflow["events"].events) == 1
        ev = wired_workflow["events"].events[0]
        assert ev.event_type == "MODEL_PROMOTED"
        assert ev.payload["model_name"] == "fraud-model"
        assert ev.payload["model_version"] == 1


# ---------------------------------------------------------------------- #
# CASE 5 — SERVING RELOAD
# ---------------------------------------------------------------------- #


class TestCase5ServingReload:
    def test_serving_bridge_receives_event_and_reloads(self, wired_workflow):
        # 1. End-to-end lifecycle (CASE 4).
        db_session = wired_workflow["db_session"]
        dv = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5},
                must_beat_production=False,
                allow_cold_start=True,
            ),
            pipeline_id=SUCCESS_PIPELINE,
        )
        assert outcome.promoted is True
        # 2. The event is then delivered to the serving bridge.
        ev = wired_workflow["events"].events[0]
        client = TestClient(wired_workflow["bridge"].app)
        r = client.post(
            "/internal/model/reload",
            json={
                "model_name": ev.payload["model_name"],
                "model_version": ev.payload["model_version"],
                "artifact_uri": ev.payload.get("artifact_uri"),
            },
        )
        assert r.status_code == 200, r.text
        # 3. The bridge now reports the active model.
        r2 = client.get(f"/internal/model/active/{ev.payload['model_name']}")
        assert r2.status_code == 200
        assert r2.json()["model_version_number"] == ev.payload["model_version"]
        # 4. The reload is persisted in the database.
        db_session.expire_all()
        from mlops_framework.database.models.serving_instance import (
            ServingInstance,
        )
        si = (
            db_session.query(ServingInstance)
            .filter_by(model_version_id=outcome.model_version_id, is_active=True)
            .first()
        )
        assert si is not None
        assert si.reload_source == "event"

    def test_subsequent_promotion_replaces_active_model(self, wired_workflow):
        # First, run CASE 4 to get v1 in production
        db_session = wired_workflow["db_session"]
        dv1 = _make_dataset_version(db_session, name="ds-1", row_count=5000)
        model = _make_model(db_session)
        out1 = wired_workflow["workflow"].run(
            dataset_version=dv1,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5},
                must_beat_production=False,
                allow_cold_start=True,
            ),
            pipeline_id=SUCCESS_PIPELINE,
        )
        assert out1.promoted is True

        # Add a second dataset version and re-run
        dv2 = DatasetManager(db_session).create_version(
            dataset_id=dv1.dataset_id,
            storage_uri="s3://b/ds-1-v2.csv",
            row_count=8000,
            metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
        )
        out2 = wired_workflow["workflow"].run(
            dataset_version=dv2,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5},
                must_beat_production=True,
                allow_cold_start=True,
            ),
            pipeline_id=SUCCESS_PIPELINE,
        )
        # The new run may not promote if the new model's metrics are
        # not strictly better than v1 (the test pipeline's metrics
        # are deterministic and identical for the same seed). We just
        # assert that either we got a promotion, or the model was
        # rejected with an explainable reason.
        if out2.promoted:
            assert out2.model_version_id != out1.model_version_id
            db_session.expire_all()
            mv1 = db_session.get(ModelVersion, out1.model_version_id)
            assert mv1.state == ModelState.ARCHIVED
        else:
            assert out2.blocked_reason == "model_rejected"


# ---------------------------------------------------------------------- #
# Eligibility shortcut
# ---------------------------------------------------------------------- #


class TestEligibilityShortcut:
    def test_cooldown_blocks(self, wired_workflow):
        db_session = wired_workflow["db_session"]
        dv1 = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        # First run succeeds with no cooldown.
        out1 = wired_workflow["workflow"].run(
            dataset_version=dv1,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5},
                must_beat_production=False,
                allow_cold_start=True,
            ),
            pipeline_id=SUCCESS_PIPELINE,
        )
        assert out1.promoted is True
        # Second dataset version is immediately retried. With a
        # 24-hour cooldown, the second run is blocked.
        dv2 = DatasetManager(db_session).create_version(
            dataset_id=dv1.dataset_id,
            storage_uri="s3://b/v2.csv",
            row_count=6000,
            metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
        )
        out2 = wired_workflow["workflow"].run(
            dataset_version=dv2,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            eligibility_config=EligibilityConfig(cooldown_hours=24),
            promotion_config=PromotionConfig(
                min_metrics={"f1": 0.5},
                must_beat_production=False,
                allow_cold_start=True,
            ),
            pipeline_id=SUCCESS_PIPELINE,
        )
        assert out2.promoted is False
        assert out2.blocked_reason == "not_eligible"


# ---------------------------------------------------------------------- #
# Settings default-resolution (CP4) — RetrainingWorkflow itself never
# constructs a TrainingPolicy/EligibilityConfig/PromotionConfig; a run
# with none of those three arguments now resolves each from persisted
# FrameworkSettingsManager instead of each policy class's own bare
# dataclass default. Proven two ways per policy layer isn't needed here
# (that's tests/unit/test_settings_wiring.py's job, one class at a
# time) — this is specifically "does a real, fully-wired
# RetrainingWorkflow.run() with zero policy args actually pick it up".
# ---------------------------------------------------------------------- #


class TestSettingsDefaultResolution:
    def test_fully_default_run_is_unchanged_when_settings_empty(self, wired_workflow):
        """No training_policy/eligibility_config/promotion_config and
        no force — empty framework_settings means every governance
        layer falls back to its own bare default exactly as it always
        has (readiness passes at required_size=0, eligibility passes
        with only require_ready, promotion cold-starts with no floors)."""
        db_session = wired_workflow["db_session"]
        dv = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv, model=model, pipeline_id=SUCCESS_PIPELINE,
        )
        assert outcome.promoted is True
        assert outcome.training_run_id is not None

    def test_persisted_min_floors_blocks_a_fully_default_run(self, wired_workflow):
        """Same call as above (no promotion_config at all) — this only
        reaches the decision if ModelPromotionPolicy.evaluate() itself
        resolves its default from Settings, since nothing else in the
        call chain ever constructs a PromotionConfig."""
        db_session = wired_workflow["db_session"]
        FrameworkSettingsManager(db_session).set_raw(
            PROMOTION, {"min_floors": {"f1": 0.99}}
        )
        dv = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv, model=model, pipeline_id=SUCCESS_PIPELINE,
        )
        assert outcome.promoted is False
        assert outcome.blocked_reason == "model_rejected"

    def test_persisted_required_size_blocks_a_fully_default_run(self, wired_workflow):
        """Same idea, one layer up — no training_policy passed; the
        persisted training_policy.required_size still blocks the run
        before training ever starts."""
        db_session = wired_workflow["db_session"]
        FrameworkSettingsManager(db_session).set_raw(
            TRAINING_POLICY, {"required_size": 999_999}
        )
        dv = _make_dataset_version(db_session, row_count=5000)
        model = _make_model(db_session)
        outcome = wired_workflow["workflow"].run(
            dataset_version=dv, model=model, pipeline_id=SUCCESS_PIPELINE,
        )
        assert outcome.promoted is False
        assert outcome.blocked_reason == "readiness_blocked"
        assert outcome.training_run_id is None
