"""The human-in-the-loop gate: approval, rejection, and unreachability.

Three outcomes, and only one of them leads to a retrain. The tests
assert on what was *persisted* about each, because the audit trail is
the deliverable here — a decision nobody can reconstruct afterwards is
not governance.
"""

from __future__ import annotations

from demo.steps import request_approval
from mlops_framework.approval import ApprovalDecision, RecordedDecisionGate
from mlops_framework.database.models.audit_log import AuditLog
from mlops_framework.database.models.governance_event import GovernanceEvent


class TestApprovalPath:
    def test_approval_is_recorded_with_the_responder(self, ctx_drifted):
        decision = request_approval.run(
            ctx_drifted, ctx_drifted.drift_result, mode="approve"
        )
        assert decision.approved is True

        with ctx_drifted.db.get_session() as session:
            rows = session.query(AuditLog).filter_by(
                action="RETRAIN_REQUEST_APPROVED"
            ).all()
        assert len(rows) == 1
        assert rows[0].entity_id == ctx_drifted.model_id
        assert rows[0].actor == "demo:auto-approve"

    def test_the_audit_row_links_the_drift_event_to_the_decision(
        self, ctx_drifted
    ):
        """The chain 'this drift caused this retrain' has to survive in
        the database, not just in the terminal."""
        import json

        request_approval.run(ctx_drifted, ctx_drifted.drift_result, mode="approve")
        with ctx_drifted.db.get_session() as session:
            row = session.query(AuditLog).filter_by(
                action="RETRAIN_REQUEST_APPROVED"
            ).one()
            meta = json.loads(row.metadata_json)

        assert meta["drift_event_id"] == ctx_drifted.state.drift_event_id
        assert meta["current_model_version_id"] == ctx_drifted.v1_model_version_id
        assert meta["drifted_window_version_id"] == (
            ctx_drifted.drifted_window_version_id
        )

    def test_state_moves_to_approved(self, ctx_drifted):
        request_approval.run(ctx_drifted, ctx_drifted.drift_result, mode="approve")
        assert ctx_drifted.state.approval_status == "APPROVED"
        assert ctx_drifted.state.retraining_status == "REQUESTED"


class TestRejectionPath:
    def test_rejection_is_recorded(self, ctx_drifted):
        decision = request_approval.run(
            ctx_drifted, ctx_drifted.drift_result, mode="reject"
        )
        assert decision.approved is False

        with ctx_drifted.db.get_session() as session:
            rows = session.query(AuditLog).filter_by(
                action="RETRAIN_REQUEST_REJECTED"
            ).all()
        assert len(rows) == 1

    def test_rejection_raises_a_governance_event(self, ctx_drifted):
        request_approval.run(ctx_drifted, ctx_drifted.drift_result, mode="reject")
        with ctx_drifted.db.get_session() as session:
            events = session.query(GovernanceEvent).filter_by(
                event_type="RUN_BLOCKED"
            ).all()
        assert any("rejected" in e.message for e in events)

    def test_state_moves_to_rejected_and_cancels_retraining(self, ctx_drifted):
        request_approval.run(ctx_drifted, ctx_drifted.drift_result, mode="reject")
        assert ctx_drifted.state.approval_status == "REJECTED"
        assert ctx_drifted.state.retraining_status == "CANCELLED"


class TestNotificationFailure:
    def test_unconfigured_telegram_denies_rather_than_raising(self, ctx_drifted):
        """An admin who was never reached has not said yes."""
        decision = request_approval.run(
            ctx_drifted, ctx_drifted.drift_result, mode="telegram"
        )
        assert decision.approved is False
        assert "unavailable" in decision.reason

    def test_the_failure_is_recorded_without_touching_the_drift_event(
        self, ctx_drifted
    ):
        from mlops_framework.database.models.drift_evaluation import (
            DriftEvaluation,
            DriftOutcome,
        )

        request_approval.run(ctx_drifted, ctx_drifted.drift_result, mode="telegram")
        with ctx_drifted.db.get_session() as session:
            events = session.query(GovernanceEvent).all()
            drift_rows = (
                session.query(DriftEvaluation)
                .filter_by(outcome=DriftOutcome.DRIFT_DETECTED)
                .all()
            )
        assert any("could not be delivered" in e.message for e in events)
        # The observation is still a valid record of what happened.
        assert len(drift_rows) == 1

    def test_a_channel_that_raises_mid_request_still_denies(
        self, ctx_drifted, monkeypatch
    ):
        class _Exploding:
            def request_approval(self, request, *, timeout=3600.0):
                raise RuntimeError("telegram exploded")

        monkeypatch.setattr(
            request_approval,
            "_resolve_gate",
            lambda ctx, mode: (_Exploding(), "Telegram"),
        )
        decision = request_approval.run(
            ctx_drifted, ctx_drifted.drift_result, mode="telegram"
        )
        assert decision.approved is False
        assert "telegram exploded" in decision.reason


class TestAlertContent:
    def test_the_alert_carries_the_facts_needed_to_decide(self, ctx_drifted):
        alert = request_approval.build_alert(ctx_drifted, ctx_drifted.drift_result)
        assert ctx_drifted.config.model_name in alert
        assert f"drift_event_{ctx_drifted.state.drift_event_id}" in alert
        assert "Drift score" in alert

    def test_the_alert_leaks_no_secrets_or_paths(self, ctx_drifted):
        """An alert is the least-controlled channel the system has."""
        ctx_drifted.settings.telegram_bot_token = "SECRET-TOKEN-123"
        alert = request_approval.build_alert(ctx_drifted, ctx_drifted.drift_result)

        assert "SECRET-TOKEN-123" not in alert
        assert str(ctx_drifted.config.data_dir) not in alert
        assert "sqlite" not in alert.lower()


class TestRecordedDecisionGate:
    """The adapter that lets the human be asked once."""

    def test_it_replays_an_approval(self):
        decision = ApprovalDecision(
            approved=True, reason="looks right", responder="alice"
        )
        replayed = RecordedDecisionGate(decision).request_approval(None)
        assert replayed.approved is True
        assert replayed.responder == "alice"

    def test_it_replays_a_denial_rather_than_auto_approving(self):
        """It is not an AutoApproveGate in disguise."""
        decision = ApprovalDecision(approved=False, reason="no", responder="bob")
        replayed = RecordedDecisionGate(decision).request_approval(None)
        assert replayed.approved is False
