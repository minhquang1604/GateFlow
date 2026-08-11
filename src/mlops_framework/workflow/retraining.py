"""Automated retraining workflow (Week 3, Day 19).

The framework owns the end-to-end lifecycle. The workflow is a single
class that orchestrates:

    1. Dataset readiness
    2. Drift detection (optional, framework-agnostic)
    3. Training eligibility
    4. Training (delegated to the existing :class:`TrainingService`)
    5. Model evaluation
    6. Promotion policy
    7. Event publishing on PROMOTION

The orchestrator only *executes* the training; the workflow makes all
the governance decisions. This keeps the responsibilities clear:

    * Framework — policy, governance, lineage.
    * Orchestrator — execute a pipeline.
    * Tracker — record experiments.

The workflow is testable: every step returns a :class:`StepResult`
that captures the outcome, the decision, and an explainable reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_promotion_event import (
    ModelPromotionEvent,
    ModelPromotionStatus,
)
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.training_run import (
    TrainingRun,
)
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.drift.detector import DriftService
from mlops_framework.events.publisher import (
    EventPublisher,
    ModelPromotedEvent,
)
from mlops_framework.governance.eligibility import (
    EligibilityConfig,
    TrainingEligibilityPolicy,
)
from mlops_framework.governance.promotion import (
    ModelPromotionPolicy,
    PromotionConfig,
)
from mlops_framework.model.manager import ModelManager
from mlops_framework.readiness.engine import ReadinessEngine, TrainingPolicy
from mlops_framework.tracking import mlflow_registry as regsync
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------- #
# Data classes
# ---------------------------------------------------------------------- #


@dataclass
class StepResult:
    """Outcome of a single workflow step."""

    name: str
    passed: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "data": dict(self.data),
        }


@dataclass
class RetrainingOutcome:
    """Aggregate outcome of a retraining workflow execution."""

    dataset_version_id: int
    model_id: int | None
    training_run_id: int | None
    model_version_id: int | None
    promotion_event_id: int | None
    steps: list[StepResult]
    promoted: bool
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_version_id": self.dataset_version_id,
            "model_id": self.model_id,
            "training_run_id": self.training_run_id,
            "model_version_id": self.model_version_id,
            "promotion_event_id": self.promotion_event_id,
            "steps": [s.to_dict() for s in self.steps],
            "promoted": self.promoted,
            "blocked_reason": self.blocked_reason,
        }


# ---------------------------------------------------------------------- #
# Workflow
# ---------------------------------------------------------------------- #


class RetrainingWorkflow:
    """Framework-controlled automated retraining workflow.

    The workflow accepts a :class:`DatasetVersion` and a target
    :class:`Model` and decides whether to retrain, run training,
    evaluate the resulting model, and (potentially) promote it.

    Hooks (all optional) allow callers to provide domain data for
    drift detection and to customize how the candidate model is
    produced.
    """

    def __init__(
        self,
        session: Session,
        *,
        training_service: TrainingService,
        readiness_engine: ReadinessEngine | None = None,
        eligibility_policy: TrainingEligibilityPolicy | None = None,
        promotion_policy: ModelPromotionPolicy | None = None,
        drift_service: DriftService | None = None,
        event_publisher: EventPublisher | None = None,
        event_session: Session | None = None,
    ) -> None:
        self._session = session
        self._service = training_service
        self._readiness = readiness_engine or ReadinessEngine(session)
        self._eligibility = eligibility_policy or TrainingEligibilityPolicy(
            session
        )
        self._promotion = promotion_policy or ModelPromotionPolicy()
        self._drift_service = drift_service
        self._event_publisher = event_publisher
        # Separate session for persisting events (may be the same as
        # self._session).
        self._event_session = event_session or session

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run(
        self,
        *,
        dataset_version: DatasetVersion,
        model: ModelRow,
        training_policy: TrainingPolicy | dict[str, Any] | None = None,
        eligibility_config: EligibilityConfig | dict[str, Any] | None = None,
        promotion_config: PromotionConfig | dict[str, Any] | None = None,
        # Hooks
        reference_data: dict[str, list[float]] | None = None,
        current_data: dict[str, list[float]] | None = None,
        pipeline_id: str = "tests._pipelines.e2e_training:main",
        evaluate_model: Callable[[ModelVersion], dict[str, Any]] | None = None,
        force: bool = False,
    ) -> RetrainingOutcome:
        """Run the full retraining workflow.

        Args:
            dataset_version: The candidate dataset version to train on.
            model: The target :class:`Model`.
            training_policy: Policy used by the readiness engine.
            eligibility_config: Policy used by the eligibility step.
            promotion_config: Policy used by the promotion step.
            reference_data / current_data: Optional, used by the
                drift detector. If both are provided, drift detection
                runs; otherwise the eligibility step treats drift as
                *unknown* (its drift-related gates become no-ops).
            pipeline_id: The orchestrator pipeline to trigger.
            evaluate_model: Optional callable that computes the metrics
                of a freshly-trained :class:`ModelVersion`. If not
                provided, the workflow uses the metrics that the
                pipeline attached to the run metadata.
            force: Force the eligibility step to allow retraining.

        Returns:
            :class:`RetrainingOutcome` with the full step trace.
        """
        steps: list[StepResult] = []

        # 1. Readiness -----------------------------------------------------
        readiness_result = self._readiness.evaluate(
            dataset_version, training_policy
        )
        if not readiness_result.is_ready:
            steps.append(
                StepResult(
                    "readiness",
                    False,
                    detail="; ".join(readiness_result.reasons) or "BLOCKED",
                    data=readiness_result.to_dict(),
                )
            )
            return self._finalize(
                dataset_version,
                model,
                steps,
                blocked_reason="readiness_blocked",
            )
        steps.append(
            StepResult(
                "readiness",
                True,
                detail="READY",
                data=readiness_result.to_dict(),
            )
        )

        # 2. Drift detection (optional) ------------------------------------
        drift_result = None
        if self._drift_service is not None and (
            reference_data is not None and current_data is not None
        ):
            # Need a reference version. Pick the most recent version of
            # the same dataset (excluding the candidate).
            reference_version = self._reference_version(
                dataset_version
            )
            if reference_version is not None:
                drift_result = self._drift_service.evaluate(
                    reference_version=reference_version,
                    current_version=dataset_version,
                    reference_data=reference_data,
                    current_data=current_data,
                )
                steps.append(
                    StepResult(
                        "drift",
                        True,
                        detail=(
                            "drift detected" if drift_result.drift_detected
                            else "no drift"
                        ),
                        data=drift_result.to_dict(),
                    )
                )

        # 3. Eligibility ---------------------------------------------------
        eligibility_ctx = self._eligibility.build_context(
            dataset_version=dataset_version,
            readiness=readiness_result,
            drift=drift_result,
            model=model,
            force=force,
        )
        eligibility_decision = self._eligibility.evaluate(
            eligibility_ctx, eligibility_config
        )
        if not eligibility_decision.eligible:
            steps.append(
                StepResult(
                    "eligibility",
                    False,
                    detail="; ".join(eligibility_decision.reasons),
                    data=eligibility_decision.to_dict(),
                )
            )
            return self._finalize(
                dataset_version,
                model,
                steps,
                blocked_reason="not_eligible",
            )
        steps.append(
            StepResult(
                "eligibility",
                True,
                detail="eligible",
                data=eligibility_decision.to_dict(),
            )
        )

        # 4. Training ------------------------------------------------------
        mm = ModelManager(self._session)
        # Use the dataset manager from the existing service if any.
        dm = DatasetManager(self._session)
        tm = TrainingManager(self._session, dm)
        run = self._service.create_run(
            dataset_version_id=dataset_version.id,
            pipeline_id=pipeline_id,
            trigger_type="DRIFT" if drift_result is not None and drift_result.drift_detected else "SCHEDULED",
        )
        try:
            self._service.start_run(run.id)
            self._service.wait_for_completion(run.id, timeout=60.0)
        except Exception as exc:
            tm.fail_run(run.id, error_message=str(exc))
            steps.append(
                StepResult(
                    "training",
                    False,
                    detail=str(exc),
                    data={"training_run_id": run.id},
                )
            )
            return self._finalize(
                dataset_version,
                model,
                steps,
                training_run_id=run.id,
                blocked_reason="training_failed",
            )
        run = tm.get_run(run.id)
        if run.status.value != "SUCCESS":
            steps.append(
                StepResult(
                    "training",
                    False,
                    detail=(
                        f"training ended in status "
                        f"{run.status.value}"
                    ),
                    data={
                        "training_run_id": run.id,
                        "error_message": run.error_message,
                    },
                )
            )
            return self._finalize(
                dataset_version,
                model,
                steps,
                training_run_id=run.id,
                blocked_reason="training_failed",
            )
        steps.append(
            StepResult(
                "training",
                True,
                detail="SUCCESS",
                data={"training_run_id": run.id},
            )
        )

        # 5. Register a ModelVersion (CANDIDATE) ---------------------------
        candidate_metrics = self._resolve_candidate_metrics(
            run, evaluate_model
        )
        mv = mm.create_model_version(
            model_id=model.id,
            dataset_version_id=dataset_version.id,
            training_run_id=run.id,
            mlflow_run_id=run.mlflow_run_id,
            state=ModelState.CANDIDATE,
            metrics=candidate_metrics,
        )
        # Registered on MLflow's side the moment the candidate exists,
        # same as the Airflow-DAG path (api/routers/internal.py's
        # promote_model) — never blocks or fails this workflow; see
        # mlflow_registry's module docstring.
        mlflow_version = regsync.sync_candidate(model.name, run.mlflow_run_id)

        # 6. Promotion policy ----------------------------------------------
        production = self._production_for_model(model.id)
        decision = self._promotion.evaluate(
            context=type("Ctx", (), {"candidate": mv, "production": production})(),
            config=promotion_config,
        )
        if not decision.approved:
            mm.transition_state(mv.id, ModelState.REJECTED)
            steps.append(
                StepResult(
                    "promotion",
                    False,
                    detail="; ".join(decision.reasons),
                    data=decision.to_dict(),
                )
            )
            return self._finalize(
                dataset_version,
                model,
                steps,
                training_run_id=run.id,
                model_version_id=mv.id,
                blocked_reason="model_rejected",
            )

        mm.transition_state(mv.id, ModelState.APPROVED)
        # Archive the prior production version (if any) *before* promoting
        # the new one. Promoting first, archiving second (the old order)
        # left a window — however brief — with two PRODUCTION versions for
        # the same model at once; nothing downstream should ever be able to
        # observe that.
        if production is not None and production.id != mv.id:
            mm.transition_state(production.id, ModelState.ARCHIVED)
        mm.transition_state(mv.id, ModelState.PRODUCTION)
        regsync.sync_production(model.name, mlflow_version)

        steps.append(
            StepResult(
                "promotion",
                True,
                detail="APPROVED → PRODUCTION",
                data=decision.to_dict(),
            )
        )

        # 7. Event ---------------------------------------------------------
        event_id = self._publish_promotion(mv)
        steps.append(
            StepResult(
                "event",
                event_id is not None,
                detail=(
                    "MODEL_PROMOTED published"
                    if event_id is not None
                    else "no publisher configured"
                ),
                data={"event_id": event_id} if event_id is not None else {},
            )
        )

        return self._finalize(
            dataset_version,
            model,
            steps,
            training_run_id=run.id,
            model_version_id=mv.id,
            promotion_event_id=event_id,
            promoted=True,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _reference_version(
        self, dataset_version: DatasetVersion
    ) -> DatasetVersion | None:
        """Pick the most recent prior version of the same dataset."""
        from sqlalchemy import select

        return self._session.execute(
            select(DatasetVersion)
            .where(
                DatasetVersion.dataset_id == dataset_version.dataset_id,
                DatasetVersion.id != dataset_version.id,
            )
            .order_by(DatasetVersion.version_number.desc())
            .limit(1)
        ).scalars().first()

    def _resolve_candidate_metrics(
        self,
        run: TrainingRun,
        evaluate_model: Callable[[ModelVersion], dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Find the metrics for the just-finished training run.

        Order of preference:
            1. Caller-supplied ``evaluate_model`` hook.
            2. Explicit ``metrics`` key on the run's metadata.
            3. The orchestrator's execution-status metadata (where the
               pipeline's stdout payload is captured).
            4. Empty dict.
        """
        if evaluate_model is not None:
            # The caller is responsible for instantiating a ModelVersion
            # for the hook. We don't have one yet, so we let the caller
            # pull metrics from anywhere it wants.
            mv_proxy = type(
                "MVProxy",
                (),
                {"dataset_version_id": run.dataset_version_id, "metrics_json": None},
            )()
            return dict(evaluate_model(mv_proxy) or {})
        try:
            meta_blob = json.loads(run.metadata_json or "{}")
        except (ValueError, TypeError):
            meta_blob = {}
        if "metrics" in meta_blob and isinstance(meta_blob["metrics"], dict):
            return dict(meta_blob["metrics"])
        # Fall back to the orchestrator's recorded metadata.
        execution_id = meta_blob.get("orchestrator_execution_id")
        if execution_id:
            try:
                exec_status = self._service._orchestrator.get_execution_status(  # noqa: SLF001
                    execution_id
                )
                metrics = (exec_status.metadata or {}).get("metrics")
                if isinstance(metrics, dict):
                    return dict(metrics)
            except Exception:
                pass
        return {}

    def _production_for_model(self, model_id: int) -> ModelVersion | None:
        from sqlalchemy import select

        return self._session.execute(
            select(ModelVersion)
            .where(
                ModelVersion.model_id == model_id,
                ModelVersion.state == ModelState.PRODUCTION,
            )
            .limit(1)
        ).scalars().first()

    def _publish_promotion(self, mv: ModelVersion) -> int | None:
        """Persist a :class:`ModelPromotionEvent` and call the publisher."""
        if self._event_publisher is None:
            return None
        # The session is potentially the same as self._session — that's fine.
        model = self._session.get(ModelRow, mv.model_id)
        if model is None:
            return None
        metrics = json.loads(mv.metrics_json) if mv.metrics_json else {}
        try:
            metrics_dict = {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}
        except (TypeError, ValueError):
            metrics_dict = {}
        event_row = ModelPromotionEvent(
            event_type="MODEL_PROMOTED",
            model_id=model.id,
            model_version_id=mv.id,
            model_name=model.name,
            model_version_number=mv.version_number,
            artifact_uri=mv.artifact_uri,
            metrics_json=json.dumps(metrics_dict) if metrics_dict else None,
            status=ModelPromotionStatus.PENDING,
        )
        self._event_session.add(event_row)
        self._event_session.flush()

        event = ModelPromotedEvent(
            model_name=model.name,
            model_version=mv.version_number,
            artifact_uri=mv.artifact_uri,
            metrics=metrics_dict,
        )
        published = self._event_publisher.publish(event)
        if published:
            event_row.status = ModelPromotionStatus.PUBLISHED
            event_row.published_at = _now().isoformat()
        else:
            event_row.status = ModelPromotionStatus.FAILED
            event_row.error_message = "publisher returned False"
        self._event_session.flush()
        return event_row.id

    def _finalize(
        self,
        dataset_version: DatasetVersion,
        model: ModelRow,
        steps: list[StepResult],
        *,
        training_run_id: int | None = None,
        model_version_id: int | None = None,
        promotion_event_id: int | None = None,
        promoted: bool = False,
        blocked_reason: str | None = None,
    ) -> RetrainingOutcome:
        return RetrainingOutcome(
            dataset_version_id=dataset_version.id,
            model_id=model.id,
            training_run_id=training_run_id,
            model_version_id=model_version_id,
            promotion_event_id=promotion_event_id,
            steps=steps,
            promoted=promoted,
            blocked_reason=blocked_reason,
        )
