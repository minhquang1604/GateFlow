"""The approval gate as a framework concern.

Human approval used to live only in a single demo script,
wired by hand into one demo. It is now an ABC the workflow depends on,
with adapters that do not depend on it — the same shape ``DriftDetector``
and ``EventPublisher`` already have.

The property that matters most is that a gate **denies by default**: a
gate that could not reach anyone has not been told yes, and failing open
would make it worse than not having one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.approval import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    AutoApproveGate,
    DenyAllGate,
)
from mlops_framework.database.base import Base
from mlops_framework.database.models import (  # noqa: F401 - registers tables
    Dataset,
    DatasetVersion,
)
from mlops_framework.database.models.audit_log import AuditLog
from mlops_framework.database.models.governance_event import GovernanceEvent
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService
from mlops_framework.workflow.retraining import RetrainingWorkflow

PIPELINE = "tests._pipelines.e2e_training:main"


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


@pytest.fixture()
def workflow_parts(session):
    dm = DatasetManager(session)
    mm = ModelManager(session)
    ds = dm.create_dataset("churn")
    version = dm.create_version(
        dataset_id=ds.id, storage_uri="s3://b/v1.csv", row_count=1000
    )
    model = mm.create_model("churn-clf")
    session.commit()

    orchestrator = LocalDockerOrchestrator()
    service = TrainingService(
        training_manager=TrainingManager(session, dm),
        orchestrator=orchestrator,
        tracker=None,
    )
    yield session, service, version, model
    orchestrator.shutdown()


class TestBuiltInGates:
    def test_auto_approve_approves(self):
        d = AutoApproveGate().request_approval(ApprovalRequest(summary="?"))
        assert d.approved is True
        assert d.responder == "auto-approve"

    def test_deny_all_denies(self):
        d = DenyAllGate().request_approval(ApprovalRequest(summary="?"))
        assert d.approved is False

    def test_the_decision_serializes_like_every_other_one(self):
        d = ApprovalDecision(approved=True, reason="ok", responder="alice")
        assert d.to_dict() == {"approved": True, "reason": "ok", "responder": "alice"}


class TestWorkflowIntegration:
    def test_no_gate_leaves_the_trace_unchanged(self, workflow_parts):
        """A workflow with no gate behaves exactly as it always has —
        the step is skipped, not auto-approved."""
        session, service, version, model = workflow_parts
        outcome = RetrainingWorkflow(session, training_service=service).run(
            dataset_version=version, model=model, pipeline_id=PIPELINE, force=True
        )
        assert "approval" not in [s.name for s in outcome.steps]

    def test_a_denial_stops_the_retrain_before_training(self, workflow_parts):
        session, service, version, model = workflow_parts
        outcome = RetrainingWorkflow(
            session, training_service=service, approval_gate=DenyAllGate()
        ).run(dataset_version=version, model=model, pipeline_id=PIPELINE, force=True)

        assert outcome.blocked_reason == "approval_denied"
        assert outcome.promoted is False
        # The point of asking before training: no compute was spent.
        assert outcome.training_run_id is None
        assert "training" not in [s.name for s in outcome.steps]

    def test_the_gate_is_asked_after_eligibility(self, workflow_parts):
        """No point asking a human about a retrain the policies have
        already ruled out."""
        session, service, version, model = workflow_parts
        outcome = RetrainingWorkflow(
            session, training_service=service, approval_gate=DenyAllGate()
        ).run(dataset_version=version, model=model, pipeline_id=PIPELINE, force=True)

        names = [s.name for s in outcome.steps]
        assert names.index("eligibility") < names.index("approval")

    def test_an_approval_lets_the_retrain_proceed(self, workflow_parts):
        session, service, version, model = workflow_parts
        outcome = RetrainingWorkflow(
            session, training_service=service, approval_gate=AutoApproveGate()
        ).run(
            dataset_version=version, model=model, pipeline_id=PIPELINE,
            force=True, training_timeout=60.0,
        )
        approval = next(s for s in outcome.steps if s.name == "approval")
        assert approval.passed is True
        assert outcome.training_run_id is not None

    def test_a_denial_is_recorded_where_a_policy_block_would_be(self, workflow_parts):
        """From everything downstream this is the same fact as a policy
        block: the retrain did not happen, and here is why."""
        session, service, version, model = workflow_parts
        RetrainingWorkflow(
            session, training_service=service, approval_gate=DenyAllGate()
        ).run(dataset_version=version, model=model, pipeline_id=PIPELINE, force=True)
        session.commit()

        events = session.query(GovernanceEvent).filter_by(event_type="RUN_BLOCKED").all()
        assert any("not approved" in e.message for e in events)

        audit = session.query(AuditLog).filter_by(action="RETRAIN_DENIED").all()
        assert len(audit) == 1
        assert audit[0].entity_id == model.id

    def test_an_approval_names_the_responder_in_the_audit_trail(self, workflow_parts):
        session, service, version, model = workflow_parts

        class _Alice(ApprovalGate):
            def request_approval(self, request, *, timeout=3600.0):
                return ApprovalDecision(
                    approved=True, reason="looks fine", responder="alice"
                )

        RetrainingWorkflow(
            session, training_service=service, approval_gate=_Alice()
        ).run(dataset_version=version, model=model, pipeline_id=PIPELINE, force=True)
        session.commit()

        row = session.query(AuditLog).filter_by(action="RETRAIN_APPROVED").one()
        assert row.actor == "alice"

    def test_the_request_carries_the_facts_a_human_needs(self, workflow_parts):
        session, service, version, model = workflow_parts
        seen: list[ApprovalRequest] = []

        class _Recording(ApprovalGate):
            def request_approval(self, request, *, timeout=3600.0):
                seen.append(request)
                return ApprovalDecision(approved=False, reason="no")

        RetrainingWorkflow(
            session, training_service=service, approval_gate=_Recording()
        ).run(dataset_version=version, model=model, pipeline_id=PIPELINE, force=True)

        assert len(seen) == 1
        assert seen[0].action == "retrain"
        assert seen[0].context["model"] == "churn-clf"
        assert seen[0].context["dataset_version_id"] == version.id
        assert "churn-clf" in seen[0].summary


class TestTelegramAdapterIsOptional:
    def test_the_framework_imports_without_a_bot_token(self):
        """Telegram is an adapter, not a dependency — same treatment
        MLflowTracker gets."""
        from mlops_framework.approval.telegram import TelegramApprovalGate

        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            TelegramApprovalGate(bot_token="", admin_chat_id="")

    def test_an_unreachable_channel_denies_rather_than_raising(self, monkeypatch):
        from mlops_framework.approval import telegram as telegram_module

        gate = telegram_module.TelegramApprovalGate(bot_token="t", admin_chat_id="1")

        def _boom(*args, **kwargs):
            raise OSError("no route to host")

        monkeypatch.setattr(gate, "_latest_update_id", _boom)
        decision = gate.request_approval(ApprovalRequest(summary="?"), timeout=1)
        assert decision.approved is False
        assert "could not reach Telegram" in decision.reason
