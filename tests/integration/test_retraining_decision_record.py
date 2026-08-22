"""The governance decision as a stored, queryable, traceable record.

Readiness and drift already wrote auditable rows. Eligibility wrote one
only when it refused, approval only as a loosely-keyed audit entry, and
the workflow's own five-gate trace was returned to the caller and
dropped. These tests pin down the record that closes that gap
(migration 012) and the lineage edges that make it reachable:

    * every exit path of the workflow writes exactly one decision row;
    * a gate that ran and said no is distinguishable from a gate that
      never ran at all (``False`` vs ``None``);
    * the row points at the readiness and drift evaluations it rested
      on, and at the run and model version it authorised;
    * lineage shows a blocked decision as a visible dead end rather than
      as nothing at all.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from mlops_framework.approval.base import (
    ApprovalDecision,
    AutoApproveGate,
    DenyAllGate,
    RecordedDecisionGate,
)
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessEvaluation,
)
from mlops_framework.database.models.retraining_decision import (
    RetrainingDecision,
    RetrainingOutcomeStatus,
)
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.events.publisher import InMemoryEventPublisher
from mlops_framework.governance.eligibility import EligibilityConfig
from mlops_framework.governance.promotion import PromotionConfig
from mlops_framework.lineage.manager import LineageManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.readiness.engine import TrainingPolicy
from mlops_framework.tracking.in_memory import InMemoryTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService
from mlops_framework.workflow.retraining import RetrainingWorkflow

SUCCESS_PIPELINE = "tests._pipelines.e2e_training:main"
FAIL_PIPELINE = "tests._pipelines.pipelines:fail"


# ---------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------- #


@pytest.fixture()
def workflow_env(db_session):
    """A wired workflow over the in-memory tracker and local orchestrator.

    Mirrors ``test_governance_end_to_end``'s fixture; kept local rather
    than shared so a change made for one file's cases cannot silently
    retune the other's.
    """
    db_session.expire_all()
    orchestrator = LocalDockerOrchestrator()
    service = TrainingService(
        training_manager=TrainingManager(db_session, DatasetManager(db_session)),
        orchestrator=orchestrator,
        tracker=InMemoryTracker(),
    )

    def build(**kwargs) -> RetrainingWorkflow:
        return RetrainingWorkflow(
            session=db_session,
            training_service=service,
            event_publisher=InMemoryEventPublisher(),
            **kwargs,
        )

    try:
        yield {"db_session": db_session, "build": build}
    finally:
        orchestrator.shutdown()


def _dataset_version(session, *, row_count: int = 5000) -> DatasetVersion:
    dm = DatasetManager(session)
    ds = dm.create_dataset(name="fraud-ds", description="d")
    return dm.create_version(
        dataset_id=ds.id,
        storage_uri="s3://b/fraud-v1.csv",
        row_count=row_count,
        metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
    )


def _model(session) -> ModelRow:
    return ModelManager(session).create_model(
        name="fraud-model", task="fraud_detection"
    )


def _decisions(session) -> list[RetrainingDecision]:
    return list(
        session.execute(
            select(RetrainingDecision).order_by(RetrainingDecision.id)
        )
        .scalars()
        .all()
    )


def _outcome_of(row: RetrainingDecision) -> str:
    return row.outcome.value if hasattr(row.outcome, "value") else str(row.outcome)


# ---------------------------------------------------------------------- #
# One row per execution, on every exit path
# ---------------------------------------------------------------------- #


class TestOneRowPerExecution:
    def test_blocked_at_readiness_writes_a_record(self, workflow_env):
        session = workflow_env["db_session"]
        dv = _dataset_version(session, row_count=10)
        outcome = workflow_env["build"]().run(
            dataset_version=dv,
            model=_model(session),
            training_policy=TrainingPolicy(required_size=1000),
            pipeline_id=SUCCESS_PIPELINE,
        )

        rows = _decisions(session)
        assert len(rows) == 1
        row = rows[0]
        assert outcome.decision_id == row.id
        assert _outcome_of(row) == RetrainingOutcomeStatus.BLOCKED.value
        assert row.blocked_reason == "readiness_blocked"
        assert row.blocked_at_step == "readiness"
        assert row.dataset_version_id == dv.id
        assert row.training_run_id is None
        assert row.model_version_id is None

    def test_promoted_run_writes_a_record_linking_run_and_version(
        self, workflow_env
    ):
        session = workflow_env["db_session"]
        dv = _dataset_version(session)
        outcome = workflow_env["build"]().run(
            dataset_version=dv,
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        assert outcome.promoted is True

        rows = _decisions(session)
        assert len(rows) == 1
        row = rows[0]
        assert _outcome_of(row) == RetrainingOutcomeStatus.PROMOTED.value
        assert row.blocked_reason is None
        assert row.blocked_at_step is None
        assert row.training_run_id == outcome.training_run_id
        assert row.model_version_id == outcome.model_version_id

    def test_each_run_appends_rather_than_overwrites(self, workflow_env):
        """History is preserved: a second attempt is a second row."""
        session = workflow_env["db_session"]
        dv = _dataset_version(session)
        model = _model(session)
        wf = workflow_env["build"]()
        for _ in range(2):
            wf.run(
                dataset_version=dv,
                model=model,
                training_policy=TrainingPolicy(required_size=100),
                pipeline_id=SUCCESS_PIPELINE,
                promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
            )
        assert len(_decisions(session)) == 2


# ---------------------------------------------------------------------- #
# "Said no" vs "never asked"
# ---------------------------------------------------------------------- #


class TestGateVerdictsAreDistinguishable:
    def test_readiness_block_leaves_later_gates_null_not_false(
        self, workflow_env
    ):
        """The gap this record exists to close.

        A run stopped at readiness never consulted eligibility or
        approval. Recording those as ``False`` would make the row claim
        two refusals that never happened — and would double-count them
        in any tally of why retrains get blocked.
        """
        session = workflow_env["db_session"]
        workflow_env["build"](approval_gate=AutoApproveGate()).run(
            dataset_version=_dataset_version(session, row_count=10),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=1000),
            pipeline_id=SUCCESS_PIPELINE,
        )
        row = _decisions(session)[0]
        assert row.eligible is None
        assert row.approved is None

    def test_eligibility_refusal_is_recorded_as_false(self, workflow_env):
        """Retrain, then immediately retrain again under a cooldown.

        The second attempt is refused by the eligibility policy — the
        "dataset is ready, but training should not happen right now"
        case the policy exists to express, and the one whose refusal
        previously reached the database only as a RunBlockedEvent.
        """
        session = workflow_env["db_session"]
        dv = _dataset_version(session)
        model = _model(session)
        wf = workflow_env["build"]()
        wf.run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        wf.run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            eligibility_config=EligibilityConfig(cooldown_hours=24),
        )

        rows = _decisions(session)
        assert len(rows) == 2
        row = rows[-1]
        assert row.eligible is False
        assert row.blocked_reason == "not_eligible"
        assert row.blocked_at_step == "eligibility"
        # Approval sits after eligibility and was never reached.
        assert row.approved is None
        assert row.training_run_id is None

    def test_eligibility_pass_is_recorded_too(self, workflow_env):
        """The half that had no storage at all before this record.

        A passing eligibility decision previously left nothing in the
        database — the framework could show why it refused a retrain but
        not why it permitted one.
        """
        session = workflow_env["db_session"]
        workflow_env["build"]().run(
            dataset_version=_dataset_version(session),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        assert _decisions(session)[0].eligible is True

    def test_denied_approval_records_the_responder(self, workflow_env):
        session = workflow_env["db_session"]
        workflow_env["build"](approval_gate=DenyAllGate()).run(
            dataset_version=_dataset_version(session),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
        )
        row = _decisions(session)[0]
        assert row.approved is False
        assert row.approval_responder == "deny-all"
        assert row.approval_reason == "denied by DenyAllGate"
        assert row.blocked_reason == "approval_denied"
        assert row.blocked_at_step == "approval"
        assert row.training_run_id is None

    def test_relayed_human_approval_keeps_the_person_not_the_relay(
        self, workflow_env
    ):
        """``RecordedDecisionGate`` exists to preserve the real
        responder when the question was asked earlier through another
        channel; the stored record must not flatten that back to the
        machinery."""
        session = workflow_env["db_session"]
        workflow_env["build"](
            approval_gate=RecordedDecisionGate(
                ApprovalDecision(
                    approved=True,
                    reason="drift confirmed by on-call",
                    responder="alice@example.com",
                )
            )
        ).run(
            dataset_version=_dataset_version(session),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        row = _decisions(session)[0]
        assert row.approved is True
        assert row.approval_responder == "alice@example.com"
        assert row.approval_reason == "drift confirmed by on-call"

    def test_no_gate_configured_is_null_not_approved(self, workflow_env):
        """No approval gate is not the same fact as an approval."""
        session = workflow_env["db_session"]
        workflow_env["build"]().run(
            dataset_version=_dataset_version(session),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        assert _decisions(session)[0].approved is None


# ---------------------------------------------------------------------- #
# Provenance links
# ---------------------------------------------------------------------- #


class TestProvenanceLinks:
    def test_record_points_at_the_readiness_row_it_rested_on(
        self, workflow_env
    ):
        session = workflow_env["db_session"]
        workflow_env["build"]().run(
            dataset_version=_dataset_version(session),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        row = _decisions(session)[0]
        assert row.readiness_evaluation_id is not None
        evaluation = session.get(ReadinessEvaluation, row.readiness_evaluation_id)
        assert evaluation is not None
        assert evaluation.dataset_version_id == row.dataset_version_id

    def test_steps_json_holds_the_whole_trace_in_order(self, workflow_env):
        session = workflow_env["db_session"]
        outcome = workflow_env["build"](approval_gate=AutoApproveGate()).run(
            dataset_version=_dataset_version(session),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        stored = json.loads(_decisions(session)[0].steps_json)
        assert [s["name"] for s in stored] == [s.name for s in outcome.steps]
        assert "readiness" in [s["name"] for s in stored]
        assert "approval" in [s["name"] for s in stored]
        # Each step keeps its own evidence, not just its verdict.
        readiness = next(s for s in stored if s["name"] == "readiness")
        assert readiness["data"]["checks"]

    def test_training_failure_links_the_run_but_no_model_version(
        self, workflow_env
    ):
        session = workflow_env["db_session"]
        outcome = workflow_env["build"]().run(
            dataset_version=_dataset_version(session),
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=FAIL_PIPELINE,
        )
        row = _decisions(session)[0]
        assert row.blocked_reason == "training_failed"
        assert row.blocked_at_step == "training"
        assert row.training_run_id == outcome.training_run_id
        assert row.model_version_id is None


# ---------------------------------------------------------------------- #
# Lineage
# ---------------------------------------------------------------------- #


class TestDecisionsInLineage:
    def test_promoted_decision_links_dataset_run_and_model(self, workflow_env):
        session = workflow_env["db_session"]
        dv = _dataset_version(session)
        outcome = workflow_env["build"](approval_gate=AutoApproveGate()).run(
            dataset_version=dv,
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        graph = LineageManager(session).graph_for_model_version(
            outcome.model_version_id
        )
        node_id = f"RetrainingDecision:{outcome.decision_id}"
        assert any(n.id == node_id for n in graph.nodes)

        edges = {(e.source, e.target, e.type) for e in graph.edges}
        assert (f"DatasetVersion:{dv.id}", node_id, "evaluated_by") in edges
        assert (
            node_id,
            f"TrainingRun:{outcome.training_run_id}",
            "authorized",
        ) in edges
        # The link the artifact chain alone could never provide: why this
        # model version was allowed into production.
        assert (
            node_id,
            f"ModelVersion:{outcome.model_version_id}",
            "promoted",
        ) in edges

    def test_blocked_decision_is_a_visible_dead_end(self, workflow_env):
        """A refused retrain used to leave no lineage trace at all, and
        was indistinguishable from a retrain nobody attempted."""
        session = workflow_env["db_session"]
        dv = _dataset_version(session)
        outcome = workflow_env["build"](approval_gate=DenyAllGate()).run(
            dataset_version=dv,
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
        )
        graph = LineageManager(session).graph_for_dataset_version(dv.id)

        node_id = f"RetrainingDecision:{outcome.decision_id}"
        node = next((n for n in graph.nodes if n.id == node_id), None)
        assert node is not None
        assert node.attributes["outcome"] == "BLOCKED"
        assert node.attributes["blocked_at_step"] == "approval"
        assert node.attributes["approved"] is False
        assert "blocked at approval" in node.label

        incoming = [e for e in graph.edges if e.target == node_id]
        outgoing = [e for e in graph.edges if e.source == node_id]
        assert [e.type for e in incoming] == ["evaluated_by"]
        assert outgoing == []

    def test_rejected_model_is_not_labelled_promoted(self, workflow_env):
        """A model the promotion policy rejected still has a model
        version; the edge to it must not claim it was promoted."""
        session = workflow_env["db_session"]
        dv = _dataset_version(session)
        outcome = workflow_env["build"]().run(
            dataset_version=dv,
            model=_model(session),
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            # The pipeline reports f1 ~0.8, below this floor.
            promotion_config=PromotionConfig(min_metrics={"f1": 0.99}),
        )
        assert outcome.blocked_reason == "model_rejected"
        assert outcome.model_version_id is not None

        graph = LineageManager(session).graph_for_dataset_version(dv.id)
        node_id = f"RetrainingDecision:{outcome.decision_id}"
        edges = {(e.source, e.target, e.type) for e in graph.edges}
        assert (
            node_id,
            f"ModelVersion:{outcome.model_version_id}",
            "rejected",
        ) in edges
        assert (
            node_id,
            f"ModelVersion:{outcome.model_version_id}",
            "promoted",
        ) not in edges

    def test_every_attempt_on_a_version_appears_side_by_side(
        self, workflow_env
    ):
        """Two attempts, one refused and one promoted, both visible on
        the same dataset version — the history, not just the survivor."""
        session = workflow_env["db_session"]
        dv = _dataset_version(session)
        model = _model(session)
        refused = workflow_env["build"](approval_gate=DenyAllGate()).run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
        )
        promoted = workflow_env["build"](approval_gate=AutoApproveGate()).run(
            dataset_version=dv,
            model=model,
            training_policy=TrainingPolicy(required_size=100),
            pipeline_id=SUCCESS_PIPELINE,
            promotion_config=PromotionConfig(min_metrics={"f1": 0.1}),
        )
        graph = LineageManager(session).graph_for_dataset_version(dv.id)
        ids = {n.id for n in graph.nodes}
        assert f"RetrainingDecision:{refused.decision_id}" in ids
        assert f"RetrainingDecision:{promoted.decision_id}" in ids
