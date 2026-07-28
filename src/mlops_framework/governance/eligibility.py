"""Training eligibility policy (Week 3, Day 16).

A dataset can be READY without implying that training *should* happen
right now. The framework therefore distinguishes:

    Dataset READY   ->  Training eligibility  ->  Training trigger

The policy implemented here evaluates:

    1. Whether the dataset is READY (from :class:`ReadinessResult`).
    2. Whether there is enough new data relative to the last training
       run (``min_new_rows``).
    3. Whether drift has been observed (``require_drift_to_retrain`` or
       ``block_when_drift_detected``).
    4. Whether a cooldown is in effect (``cooldown_hours`` vs the
       most-recent training run on the same dataset).
    5. Whether the existing production model is "good enough" that no
       retraining is necessary (``block_when_production_metrics_meet``).
    6. Whether the existing production model beats a minimum quality
       floor (``require_production_below``).

Every policy field is optional. With no fields configured, the policy
just checks the readiness result and the cooldown.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.training_run import (
    RunStatus,
    TrainingRun,
)
from mlops_framework.drift.detector import DriftResult
from mlops_framework.readiness.engine import (
    ReadinessResult,
    ReadinessStatus,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------- #
# Configuration & decision
# ---------------------------------------------------------------------- #


@dataclass
class EligibilityConfig:
    """Configuration for the training-eligibility policy.

    Every field is optional. ``None`` means "do not evaluate this
    criterion".
    """

    # Hard requirement: dataset must be READY. Defaults to True.
    require_ready: bool = True
    # Minimum new rows since the most recent training run on the same
    # dataset. ``None`` disables the check.
    min_new_rows: Optional[int] = None
    # If True, training is only allowed when a recent drift evaluation
    # detected drift. ``None`` disables.
    require_drift_to_retrain: Optional[bool] = None
    # If True, training is blocked when recent drift evaluation
    # detected drift. ``None`` disables.
    block_when_drift_detected: Optional[bool] = None
    # Minimum gap between two training runs on the same dataset, in
    # hours. ``None`` disables.
    cooldown_hours: Optional[float] = None
    # If provided, training is blocked when the existing production
    # model already meets all these metrics. ``None`` disables.
    block_when_production_metrics_meet: Optional[dict[str, float]] = None
    # If provided, training is only allowed when the existing
    # production model is *below* these metrics on at least one key.
    # ``None`` disables.
    require_production_below: Optional[dict[str, float]] = None
    # If True, retraining is blocked when no production model exists
    # yet (i.e. the very first training must be triggered explicitly
    # rather than by the workflow). ``None`` disables.
    require_existing_production: Optional[bool] = None
    # If True, retraining is blocked when a production model exists
    # (use this to control a "first training only" workflow). ``None``
    # disables.
    block_when_production_exists: Optional[bool] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EligibilityConfig":
        if data is None:
            return cls()
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class EligibilityContext:
    """Inputs to the eligibility decision.

    The framework collects these inputs; the policy does not have to
    know about the database.
    """

    readiness: Optional[ReadinessResult] = None
    drift: Optional[DriftResult] = None
    # Most recent training run on the same dataset (if any).
    last_training_run: Optional[TrainingRun] = None
    # The most recent model version (if any), regardless of state.
    last_model_version: Optional[ModelVersion] = None
    # The currently production model version (if any).
    production_model_version: Optional[ModelVersion] = None
    # Row count of the candidate dataset version.
    candidate_row_count: int = 0
    # Row count of the dataset version that produced the existing
    # production model (if any).
    production_row_count: Optional[int] = None
    # Optional explicit override (e.g. operator says "force train").
    force: bool = False


@dataclass
class EligibilityDecision:
    """Explainable eligibility decision."""

    eligible: bool
    reasons: list[str]
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "details": dict(self.details),
        }


# ---------------------------------------------------------------------- #
# Policy
# ---------------------------------------------------------------------- #


class TrainingEligibilityPolicy:
    """Reusable training-eligibility policy.

    Stateless except for the database session. The policy evaluates
    the supplied :class:`EligibilityContext` against an
    :class:`EligibilityConfig` and returns an explainable decision.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # Convenience constructor
    # ------------------------------------------------------------------ #

    def build_context(
        self,
        *,
        dataset_version: DatasetVersion,
        readiness: Optional[ReadinessResult] = None,
        drift: Optional[DriftResult] = None,
        model: Optional[Model] = None,
        force: bool = False,
    ) -> EligibilityContext:
        """Build an :class:`EligibilityContext` for a dataset version.

        The context pulls the most-recent training run and the current
        production model version (if any) from the database.
        """
        last_run = self._most_recent_run(dataset_version.dataset_id)
        last_mv = self._most_recent_model_version(dataset_version.dataset_id)
        prod_mv = None
        if model is not None:
            prod_mv = self._production_for_model(model.id)
        production_row_count: Optional[int] = None
        if prod_mv is not None:
            dv = self._session.get(DatasetVersion, prod_mv.dataset_version_id)
            if dv is not None:
                production_row_count = dv.row_count
        return EligibilityContext(
            readiness=readiness,
            drift=drift,
            last_training_run=last_run,
            last_model_version=last_mv,
            production_model_version=prod_mv,
            candidate_row_count=dataset_version.row_count,
            production_row_count=production_row_count,
            force=force,
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        context: EligibilityContext,
        config: EligibilityConfig | dict[str, Any] | None = None,
    ) -> EligibilityDecision:
        """Evaluate ``context`` against ``config`` and return a decision.

        The decision is *always* explainable: every check contributes a
        pass/fail line. ``force=True`` short-circuits the decision to
        eligible and records the override in the reasons.
        """
        if isinstance(config, dict):
            cfg = EligibilityConfig.from_dict(config)
        elif config is None:
            cfg = EligibilityConfig()
        else:
            cfg = config

        reasons: list[str] = []
        details: dict[str, Any] = {}

        if context.force:
            return EligibilityDecision(
                eligible=True,
                reasons=["forced by operator"],
                details={"force": True},
            )

        # 1. Dataset readiness
        if cfg.require_ready:
            if context.readiness is None:
                reasons.append("No readiness evaluation provided")
                details["readiness"] = "MISSING"
            elif context.readiness.status != ReadinessStatus.READY:
                reasons.append("Dataset is not READY")
                details["readiness"] = context.readiness.status.value

        # 2. New data threshold
        if cfg.min_new_rows is not None and context.production_row_count is not None:
            new_rows = max(
                0, context.candidate_row_count - context.production_row_count
            )
            details["new_rows"] = new_rows
            if new_rows < cfg.min_new_rows:
                reasons.append(
                    f"Insufficient new data: {new_rows} new rows, "
                    f"need at least {cfg.min_new_rows}"
                )

        # 3. Drift-triggered training
        if cfg.require_drift_to_retrain and context.drift is not None:
            if not context.drift.drift_detected:
                reasons.append(
                    "No drift detected and policy requires drift to retrain"
                )

        # 4. Drift-blocked training
        if cfg.block_when_drift_detected and context.drift is not None:
            if context.drift.drift_detected:
                reasons.append(
                    "Drift detected and policy blocks retraining on drift"
                )

        # 5. Cooldown
        if cfg.cooldown_hours is not None and context.last_training_run is not None:
            last_completed = context.last_training_run.completed_at
            if last_completed is not None:
                # Defensive tz handling
                if last_completed.tzinfo is None:
                    last_completed = last_completed.replace(tzinfo=timezone.utc)
                gap = (_now() - last_completed).total_seconds() / 3600.0
                details["hours_since_last_run"] = round(gap, 2)
                if gap < cfg.cooldown_hours:
                    reasons.append(
                        f"Retraining cooldown is still active: "
                        f"{gap:.2f}h < {cfg.cooldown_hours}h"
                    )

        # 6. Production metrics already meet threshold
        if (
            cfg.block_when_production_metrics_meet
            and context.production_model_version is not None
        ):
            prod_metrics = _safe_metrics(context.production_model_version)
            details["production_metrics"] = prod_metrics
            failing = [
                m
                for m, t in cfg.block_when_production_metrics_meet.items()
                if prod_metrics.get(m) is None
                or prod_metrics.get(m) < t
            ]
            if not failing:
                reasons.append(
                    "Existing production model already meets all metric "
                    "thresholds; retraining not necessary"
                )

        # 7. Production metrics below a required floor
        if (
            cfg.require_production_below
            and context.production_model_version is not None
        ):
            prod_metrics = _safe_metrics(context.production_model_version)
            details["production_metrics"] = prod_metrics
            under = [
                m
                for m, t in cfg.require_production_below.items()
                if prod_metrics.get(m) is not None
                and prod_metrics.get(m) < t
            ]
            if not under:
                reasons.append(
                    "Existing production model is not below any of the "
                    "required floors"
                )

        # 8. Production model existence constraints
        if cfg.require_existing_production and context.production_model_version is None:
            reasons.append(
                "No production model exists yet; cannot satisfy "
                "require_existing_production"
            )
        if cfg.block_when_production_exists and context.production_model_version is not None:
            reasons.append(
                "A production model already exists; policy blocks "
                "retraining when one is present"
            )

        eligible = len(reasons) == 0
        if not eligible:
            details["evaluated_at"] = _now().isoformat()
        return EligibilityDecision(
            eligible=eligible,
            reasons=reasons,
            details=details,
        )

    # ------------------------------------------------------------------ #
    # DB helpers
    # ------------------------------------------------------------------ #

    def _most_recent_run(self, dataset_id: int) -> Optional[TrainingRun]:
        # A dataset has many versions; we look at the *most recent*
        # training run on any of them.
        return self._session.execute(
            select(TrainingRun)
            .where(
                TrainingRun.dataset_version_id.in_(
                    select(DatasetVersion.id).where(
                        DatasetVersion.dataset_id == dataset_id
                    )
                )
            )
            .order_by(TrainingRun.created_at.desc())
            .limit(1)
        ).scalars().first()

    def _most_recent_model_version(self, dataset_id: int) -> Optional[ModelVersion]:
        return self._session.execute(
            select(ModelVersion)
            .where(
                ModelVersion.dataset_version_id.in_(
                    select(DatasetVersion.id).where(
                        DatasetVersion.dataset_id == dataset_id
                    )
                )
            )
            .order_by(ModelVersion.created_at.desc())
            .limit(1)
        ).scalars().first()

    def _production_for_model(self, model_id: int) -> Optional[ModelVersion]:
        return self._session.execute(
            select(ModelVersion)
            .where(
                ModelVersion.model_id == model_id,
                ModelVersion.state == ModelState.PRODUCTION,
            )
            .limit(1)
        ).scalars().first()


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
