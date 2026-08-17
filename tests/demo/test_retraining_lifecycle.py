"""The retrain, and the invariant it must never break.

    A failed, rejected, or unapproved retrain must never replace the
    currently valid production model.

Every test below is a different way of not retraining successfully, and
every one of them asserts the same thing afterwards: V1 is still the
production model. The happy path is one test among many on purpose —
it is the case that is easy to get right.

Airflow and MLflow are substituted for a real local subprocess and the
in-memory tracker. The orchestrator changes; not one governance decision
does, which is the point of the framework's orchestrator abstraction.
"""

from __future__ import annotations

import dataclasses

import pytest

from demo.steps import build_dataset_v2, retrain
from mlops_framework.approval import ApprovalDecision
from mlops_framework.database.models.audit_log import AuditLog
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.tracking.in_memory import InMemoryTracker

APPROVED = ApprovalDecision(
    approved=True, reason="approved in test", responder="tester"
)
DENIED = ApprovalDecision(approved=False, reason="denied in test", responder="tester")

#: A pipeline that raises rather than training.
EXPLODING_PIPELINE = "tests._pipelines.pipelines:raises"


@pytest.fixture
def local_stack(monkeypatch):
    """Swap Airflow/MLflow for a local subprocess and an in-memory tracker."""
    monkeypatch.setattr(
        retrain, "AirflowOrchestrator", lambda **_: LocalDockerOrchestrator()
    )
    monkeypatch.setattr(retrain, "MLflowTracker", lambda **_: InMemoryTracker())


@pytest.fixture
def ctx_ready(ctx_drifted, local_stack):
    """Drift detected, V2 built, ready to retrain.

    The promotion floor is set to the metrics the *test* pipeline emits
    (f1 only); the demo's own config also floors precision, which the
    real XGBoost pipeline reports and this stand-in does not.
    """
    ctx_drifted.config = dataclasses.replace(
        ctx_drifted.config, promotion_min_metrics={"f1": 0.70}
    )
    build_dataset_v2.run(ctx_drifted)
    return ctx_drifted


def _states(ctx) -> dict[int, str]:
    with ctx.db.get_session() as session:
        return {
            mv.version_number: mv.state.value
            for mv in session.query(ModelVersion)
            .filter_by(model_id=ctx.model_id)
            .all()
        }


def _production(ctx):
    with ctx.db.get_session() as session:
        return (
            session.query(ModelVersion)
            .filter_by(model_id=ctx.model_id, state=ModelState.PRODUCTION)
            .all()
        )


class TestApprovedRetrain:
    """The happy path — and only after approval."""

    def test_v2_is_trained_registered_and_promoted(self, ctx_ready):
        outcome = retrain.run(ctx_ready, APPROVED)

        assert outcome.promoted is True
        assert outcome.training_run_id is not None
        assert outcome.model_version_id is not None
        assert _states(ctx_ready)[2] == "PRODUCTION"

    def test_v1_is_archived(self, ctx_ready):
        retrain.run(ctx_ready, APPROVED)
        assert _states(ctx_ready)[1] == "ARCHIVED"

    def test_exactly_one_version_is_in_production(self, ctx_ready):
        """Nothing downstream should ever observe two."""
        retrain.run(ctx_ready, APPROVED)
        assert len(_production(ctx_ready)) == 1

    def test_v2_is_linked_to_dataset_v2(self, ctx_ready):
        outcome = retrain.run(ctx_ready, APPROVED)
        with ctx_ready.db.get_session() as session:
            mv = session.get(ModelVersion, outcome.model_version_id)
        assert mv.dataset_version_id == ctx_ready.v2_version_id

    def test_the_workflow_records_the_approval_in_its_own_audit_row(
        self, ctx_ready
    ):
        """The human was asked once, but the workflow still audits it."""
        retrain.run(ctx_ready, APPROVED)
        with ctx_ready.db.get_session() as session:
            rows = session.query(AuditLog).filter_by(action="RETRAIN_APPROVED").all()
        assert len(rows) == 1
        assert rows[0].actor == "tester"

    def test_the_step_trace_covers_the_whole_chain(self, ctx_ready):
        outcome = retrain.run(ctx_ready, APPROVED)
        names = [s.name for s in outcome.steps]
        for expected in ("readiness", "drift", "eligibility", "approval", "training",
                         "promotion"):
            assert expected in names
        # The gate is consulted before any compute is spent.
        assert names.index("approval") < names.index("training")


class TestOrchestratorPortability:
    """The step must not assume one orchestrator's extras.

    ``shutdown()`` is not on the Orchestrator ABC — LocalDockerOrchestrator
    has it to reap subprocesses, AirflowOrchestrator does not. Because the
    tests above substitute the local one, an unguarded call passes every
    test here and then fails only against real Airflow, after the retrain
    has already succeeded.
    """

    def test_an_orchestrator_without_shutdown_still_completes(
        self, ctx_ready, monkeypatch
    ):
        class _NoShutdown:
            """Delegates everything except the method Airflow lacks."""

            def __init__(self):
                self._inner = LocalDockerOrchestrator()

            def __getattr__(self, name):
                if name == "shutdown":
                    raise AttributeError(name)
                return getattr(self._inner, name)

        monkeypatch.setattr(retrain, "AirflowOrchestrator", lambda **_: _NoShutdown())
        outcome = retrain.run(ctx_ready, APPROVED)
        assert outcome.promoted is True


class TestDeniedRetrain:
    def test_a_denial_reaching_the_workflow_stops_it(self, ctx_ready):
        outcome = retrain.run(ctx_ready, DENIED)

        assert outcome.promoted is False
        assert outcome.blocked_reason == "approval_denied"
        assert outcome.training_run_id is None, "no compute should be spent"

    def test_v1_remains_in_production_after_a_denial(self, ctx_ready):
        retrain.run(ctx_ready, DENIED)
        production = _production(ctx_ready)
        assert len(production) == 1
        assert production[0].id == ctx_ready.v1_model_version_id

    def test_no_model_version_is_created_by_a_denied_retrain(self, ctx_ready):
        retrain.run(ctx_ready, DENIED)
        assert set(_states(ctx_ready)) == {1}


class TestTrainingFailure:
    @pytest.fixture
    def ctx_broken(self, ctx_ready):
        ctx_ready.config = dataclasses.replace(
            ctx_ready.config,
            pipeline_id=EXPLODING_PIPELINE,
            dag_id=EXPLODING_PIPELINE,
        )
        return ctx_ready

    def test_training_failure_does_not_promote(self, ctx_broken):
        outcome = retrain.run(ctx_broken, APPROVED)
        assert outcome.promoted is False
        assert outcome.blocked_reason == "training_failed"

    def test_v1_remains_in_production_after_training_fails(self, ctx_broken):
        retrain.run(ctx_broken, APPROVED)
        production = _production(ctx_broken)
        assert len(production) == 1
        assert production[0].id == ctx_broken.v1_model_version_id

    def test_the_failure_is_recorded(self, ctx_broken):
        from mlops_framework.database.models.governance_event import GovernanceEvent

        retrain.run(ctx_broken, APPROVED)
        with ctx_broken.db.get_session() as session:
            events = session.query(GovernanceEvent).filter_by(
                event_type="TRAINING_FAILED"
            ).all()
        assert events


class TestValidationFailure:
    @pytest.fixture
    def ctx_strict(self, ctx_ready):
        """An acceptance floor the candidate cannot clear."""
        ctx_ready.config = dataclasses.replace(
            ctx_ready.config, promotion_min_metrics={"f1": 0.999}
        )
        return ctx_ready

    def test_a_candidate_below_the_floor_is_rejected(self, ctx_strict):
        outcome = retrain.run(ctx_strict, APPROVED)
        assert outcome.promoted is False
        assert outcome.blocked_reason == "model_rejected"

    def test_the_rejected_candidate_is_marked_rejected_not_promoted(
        self, ctx_strict
    ):
        """Training succeeded, so a V2 exists — it just must not serve."""
        outcome = retrain.run(ctx_strict, APPROVED)
        with ctx_strict.db.get_session() as session:
            mv = session.get(ModelVersion, outcome.model_version_id)
        assert mv.state == ModelState.REJECTED

    def test_v1_remains_in_production_after_validation_fails(self, ctx_strict):
        retrain.run(ctx_strict, APPROVED)
        production = _production(ctx_strict)
        assert len(production) == 1
        assert production[0].id == ctx_strict.v1_model_version_id

    def test_the_rejection_is_audited(self, ctx_strict):
        retrain.run(ctx_strict, APPROVED)
        with ctx_strict.db.get_session() as session:
            rows = session.query(AuditLog).filter_by(action="MODEL_REJECTED").all()
        assert len(rows) == 1


class TestRetrainingTrigger:
    def test_a_second_retrain_does_not_leave_two_production_versions(
        self, ctx_ready
    ):
        """Re-running the trigger must converge, not fork."""
        retrain.run(ctx_ready, APPROVED)
        retrain.run(ctx_ready, APPROVED)
        assert len(_production(ctx_ready)) == 1

    def test_the_run_carries_the_causal_ids_for_audit(self, ctx_ready):
        import json

        from mlops_framework.database.models.training_run import TrainingRun

        outcome = retrain.run(ctx_ready, APPROVED)
        with ctx_ready.db.get_session() as session:
            run = session.get(TrainingRun, outcome.training_run_id)
            meta = json.loads(run.metadata_json)

        trigger = meta["trigger"]
        assert trigger["drift_event_id"] == ctx_ready.state.drift_event_id
        assert trigger["approved_by"] == "tester"
        assert trigger["parent_dataset_version_id"] == ctx_ready.v1_version_id

    def test_the_internal_v2_vs_v1_check_is_diluted_not_suppressed(
        self, ctx_ready
    ):
        """V2 contains V1, so the workflow's own drift comparison is
        expected to come back quiet under the corrected threshold.

        This pins the reasoning behind ``require_drift_to_retrain=False``:
        if this step ever starts reporting drift, the dilution argument
        no longer holds and the gate should be reconsidered rather than
        left off out of habit.
        """
        outcome = retrain.run(ctx_ready, APPROVED)
        drift_step = next(s for s in outcome.steps if s.name == "drift")
        assert "no drift" in drift_step.detail
        # And it did not stop the retrain, because that is not what
        # justifies it.
        assert outcome.promoted is True

    def test_the_run_is_marked_drift_triggered(self, ctx_ready):
        from mlops_framework.database.models.training_run import TrainingRun

        outcome = retrain.run(ctx_ready, APPROVED)
        with ctx_ready.db.get_session() as session:
            run = session.get(TrainingRun, outcome.training_run_id)
        assert run.trigger_type == "DRIFT"
