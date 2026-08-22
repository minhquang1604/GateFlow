"""Dataset-readiness engine.

Evaluates a :class:`DatasetVersion` against a configurable training
policy and produces a normalized READY/BLOCKED decision. Every evaluation
is persisted to the metadata database so operators can see *why* a
dataset was blocked (auditability is mandatory).

The engine does not perform training or trigger any workflow. It only
*describes* the data; downstream policy code decides what to do with
the decision.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessCheckOutcome,
    ReadinessEvaluation,
    ReadinessStatus,
)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------- #
# Data classes
# ---------------------------------------------------------------------- #


@dataclass
class TrainingPolicy:
    """A reusable training policy.

    The fields are intentionally small and JSON-serializable so the
    same policy can live in code, configuration files, or a database.

    ``required_size``        — minimum row count.
    ``freshness_hours``      — maximum age (hours since created_at).
    ``required_columns``     — column names that must be present.
    ``dtypes``               — optional name -> dtype requirements.
    ``max_missing_ratio``    — global maximum ratio of missing values
                               (per row) allowed (0.0..1.0).
    ``validation_rules``     — boolean feature flags used as a simple,
                               human-readable spec. The engine itself
                               reads ``validation_rules.amount_not_null``
                               and ``validation_rules.class_valid`` if
                               present.
    ``expected_column_count``— optional exact column-count requirement.
    """

    required_size: int = 0
    freshness_hours: int | None = None
    required_columns: list[str] = field(default_factory=list)
    dtypes: dict[str, str] = field(default_factory=dict)
    max_missing_ratio: float | None = None
    expected_column_count: int | None = None
    validation_rules: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingPolicy:
        """Build a TrainingPolicy from a plain dict (e.g. config)."""
        if data is None:
            return cls()
        return cls(
            required_size=int(data.get("required_size", 0) or 0),
            freshness_hours=data.get("freshness_hours"),
            required_columns=list(data.get("required_columns", []) or []),
            dtypes=dict(data.get("dtypes", {}) or {}),
            max_missing_ratio=data.get("max_missing_ratio"),
            expected_column_count=data.get("expected_column_count"),
            validation_rules=dict(data.get("validation_rules", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_size": self.required_size,
            "freshness_hours": self.freshness_hours,
            "required_columns": list(self.required_columns),
            "dtypes": dict(self.dtypes),
            "max_missing_ratio": self.max_missing_ratio,
            "expected_column_count": self.expected_column_count,
            "validation_rules": dict(self.validation_rules),
        }


@dataclass
class ReadinessCheck:
    """Outcome of a single readiness check."""

    name: str
    outcome: ReadinessCheckOutcome
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "outcome": self.outcome.value, "detail": self.detail}


@dataclass
class ReadinessResult:
    """Normalized, framework-level readiness decision.

    The decision is intentionally small and explainable. Downstream
    code (TrainingEligibilityPolicy, RetrainingWorkflow) consumes it.
    """

    status: ReadinessStatus
    passed: bool
    checks: list[ReadinessCheck]
    reasons: list[str]
    policy: TrainingPolicy
    dataset_version_id: int
    evaluated_at: datetime
    observed_row_count: int
    # Primary key of the ReadinessEvaluation row this result was
    # persisted as. Set by :meth:`ReadinessEngine.evaluate` after the
    # flush; ``None`` on a result that was built but never stored (only
    # happens in tests that construct one directly). Carried so that a
    # caller holding the result — RetrainingWorkflow — can reference the
    # stored evaluation by foreign key instead of re-querying for "the
    # most recent row for this dataset version" and hoping it is the
    # same one.
    evaluation_id: int | None = None

    @property
    def is_ready(self) -> bool:
        return self.status == ReadinessStatus.READY

    def check_dict(self) -> dict[str, str]:
        return {c.name: c.outcome.value for c in self.checks}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "checks": self.check_dict(),
            "dataset_version_id": self.dataset_version_id,
            "evaluation_id": self.evaluation_id,
            "evaluated_at": self.evaluated_at.isoformat(),
            "observed_row_count": self.observed_row_count,
        }


# ---------------------------------------------------------------------- #
# Engine
# ---------------------------------------------------------------------- #


class ReadinessEngine:
    """Dataset-readiness engine.

    Stateless except for its database session. Each call to
    :meth:`evaluate` runs the configured checks, persists the
    evaluation, and returns a normalized :class:`ReadinessResult`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def evaluate(
        self,
        dataset_version: DatasetVersion,
        policy: TrainingPolicy | dict[str, Any] | None = None,
    ) -> ReadinessResult:
        """Evaluate a :class:`DatasetVersion` against ``policy``.

        Args:
            dataset_version: The dataset version to evaluate. Must be
                attached to ``self._session`` (or be loaded later).
            policy: A :class:`TrainingPolicy` or a JSON-serializable
                dict. ``None`` means *no policy* — only structural
                existence checks run.

        Returns:
            :class:`ReadinessResult`. Persisted to the DB.
        """
        if isinstance(policy, dict):
            policy_obj = TrainingPolicy.from_dict(policy)
        elif policy is None:
            # Deferred import: framework_settings.manager imports
            # TrainingPolicy from this module, so importing it back at
            # module level here would be circular. self._session is
            # always present (constructor-mandatory).
            from mlops_framework.framework_settings.manager import (
                FrameworkSettingsManager,
            )

            policy_obj = FrameworkSettingsManager(self._session).get_training_policy()
        else:
            policy_obj = policy

        checks = list(self._run_checks(dataset_version, policy_obj))
        reasons = [
            c.detail
            for c in checks
            if c.outcome == ReadinessCheckOutcome.FAILED
        ]
        passed = all(
            c.outcome != ReadinessCheckOutcome.FAILED for c in checks
        )
        status = ReadinessStatus.READY if passed else ReadinessStatus.BLOCKED
        now = _now()

        result = ReadinessResult(
            status=status,
            passed=passed,
            checks=checks,
            reasons=reasons,
            policy=policy_obj,
            dataset_version_id=dataset_version.id,
            evaluated_at=now,
            observed_row_count=dataset_version.row_count,
        )

        result.evaluation_id = self._persist(result, dataset_version).id
        return result

    def get_evaluations(
        self, dataset_version_id: int
    ) -> list[ReadinessEvaluation]:
        """Return all evaluations for a dataset version, newest first."""
        from sqlalchemy import select

        return list(
            self._session.execute(
                select(ReadinessEvaluation)
                .where(ReadinessEvaluation.dataset_version_id == dataset_version_id)
                .order_by(ReadinessEvaluation.created_at.desc())
            ).scalars().all()
        )

    # ------------------------------------------------------------------ #
    # Internal — checks
    # ------------------------------------------------------------------ #

    def _run_checks(
        self,
        version: DatasetVersion,
        policy: TrainingPolicy,
    ) -> Iterable[ReadinessCheck]:
        yield self._check_size(version, policy)
        yield self._check_freshness(version, policy)
        yield self._check_schema(version, policy)
        yield self._check_required_columns(version, policy)
        yield self._check_dtypes(version, policy)
        yield self._check_column_count(version, policy)
        yield self._check_missing_ratio(version, policy)
        yield self._check_validation_rules(version, policy)

    @staticmethod
    def _check_size(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        if policy.required_size <= 0:
            return ReadinessCheck(
                "size", ReadinessCheckOutcome.SKIPPED,
                "no minimum size configured",
            )
        if version.row_count >= policy.required_size:
            return ReadinessCheck(
                "size", ReadinessCheckOutcome.PASSED,
                f"{version.row_count} rows >= required {policy.required_size}",
            )
        return ReadinessCheck(
            "size", ReadinessCheckOutcome.FAILED,
            (
                f"Dataset contains {version.row_count} rows; "
                f"required at least {policy.required_size}"
            ),
        )

    @staticmethod
    def _check_freshness(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        if policy.freshness_hours is None:
            return ReadinessCheck(
                "freshness", ReadinessCheckOutcome.SKIPPED,
                "no freshness window configured",
            )
        now = _now()
        # `created_at` is timezone-aware (TimestampMixin). Be defensive.
        created_at = version.created_at
        if created_at is None:
            return ReadinessCheck(
                "freshness", ReadinessCheckOutcome.SKIPPED,
                "no created_at on dataset version",
            )
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age = now - created_at
        max_age = timedelta(hours=float(policy.freshness_hours))
        if age <= max_age:
            return ReadinessCheck(
                "freshness", ReadinessCheckOutcome.PASSED,
                f"age {age} within {max_age}",
            )
        return ReadinessCheck(
            "freshness", ReadinessCheckOutcome.FAILED,
            (
                f"Dataset age {age} exceeds freshness window "
                f"{policy.freshness_hours}h"
            ),
        )

    @staticmethod
    def _check_schema(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        # A schema hash is always computed on creation; we report it
        # for traceability. The engine itself cannot know the
        # "expected" schema unless one is supplied through dtypes /
        # required_columns / expected_column_count — those checks
        # cover the typed side. The structural schema is therefore
        # reported as PASSED whenever a hash is present.
        if not version.schema_hash:
            return ReadinessCheck(
                "schema", ReadinessCheckOutcome.FAILED,
                "DatasetVersion has no schema hash",
            )
        return ReadinessCheck(
            "schema", ReadinessCheckOutcome.PASSED,
            f"schema_hash={version.schema_hash[:12]}…",
        )

    @staticmethod
    def _check_required_columns(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        if not policy.required_columns:
            return ReadinessCheck(
                "required_columns", ReadinessCheckOutcome.SKIPPED,
                "no required columns configured",
            )
        observed = _observed_columns(version)
        missing = [c for c in policy.required_columns if c not in observed]
        if not missing:
            return ReadinessCheck(
                "required_columns", ReadinessCheckOutcome.PASSED,
                f"all {len(policy.required_columns)} required columns present",
            )
        return ReadinessCheck(
            "required_columns", ReadinessCheckOutcome.FAILED,
            f"missing required columns: {sorted(missing)}",
        )

    @staticmethod
    def _check_dtypes(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        if not policy.dtypes:
            return ReadinessCheck(
                "dtypes", ReadinessCheckOutcome.SKIPPED,
                "no dtype requirements configured",
            )
        observed = _observed_columns(version)
        problems: list[str] = []
        for col, expected in policy.dtypes.items():
            if col not in observed:
                problems.append(f"{col}: missing")
            elif observed[col] != expected:
                problems.append(
                    f"{col}: expected {expected}, got {observed[col]}"
                )
        if not problems:
            return ReadinessCheck(
                "dtypes", ReadinessCheckOutcome.PASSED,
                f"{len(policy.dtypes)} dtype(s) match",
            )
        return ReadinessCheck(
            "dtypes", ReadinessCheckOutcome.FAILED,
            f"dtype mismatches: {problems}",
        )

    @staticmethod
    def _check_column_count(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        if policy.expected_column_count is None:
            return ReadinessCheck(
                "column_count", ReadinessCheckOutcome.SKIPPED,
                "no expected_column_count configured",
            )
        observed = _observed_columns(version)
        if len(observed) == policy.expected_column_count:
            return ReadinessCheck(
                "column_count", ReadinessCheckOutcome.PASSED,
                f"{len(observed)} columns match expected",
            )
        return ReadinessCheck(
            "column_count", ReadinessCheckOutcome.FAILED,
            (
                f"observed {len(observed)} columns, "
                f"expected {policy.expected_column_count}"
            ),
        )

    @staticmethod
    def _check_missing_ratio(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        if policy.max_missing_ratio is None:
            return ReadinessCheck(
                "missing_ratio", ReadinessCheckOutcome.SKIPPED,
                "no max_missing_ratio configured",
            )
        ratio = _missing_ratio(version)
        if ratio is None:
            return ReadinessCheck(
                "missing_ratio", ReadinessCheckOutcome.SKIPPED,
                "missing-value ratio not available in metadata",
            )
        if ratio <= policy.max_missing_ratio:
            return ReadinessCheck(
                "missing_ratio", ReadinessCheckOutcome.PASSED,
                f"missing ratio {ratio:.4f} <= {policy.max_missing_ratio}",
            )
        return ReadinessCheck(
            "missing_ratio", ReadinessCheckOutcome.FAILED,
            (
                f"missing ratio {ratio:.4f} exceeds "
                f"max {policy.max_missing_ratio}"
            ),
        )

    @staticmethod
    def _check_validation_rules(
        version: DatasetVersion, policy: TrainingPolicy
    ) -> ReadinessCheck:
        if not policy.validation_rules:
            return ReadinessCheck(
                "validation_rules", ReadinessCheckOutcome.SKIPPED,
                "no validation_rules configured",
            )
        meta = _metadata(version)
        problems: list[str] = []
        if policy.validation_rules.get("amount_not_null"):
            null_count = meta.get("amount_null_count")
            if isinstance(null_count, int) and null_count > 0:
                problems.append(f"amount has {null_count} nulls")
        if policy.validation_rules.get("class_valid"):
            invalid = meta.get("class_invalid_count")
            if isinstance(invalid, int) and invalid > 0:
                problems.append(f"class has {invalid} invalid rows")
        if not problems:
            return ReadinessCheck(
                "validation_rules", ReadinessCheckOutcome.PASSED,
                f"{len(policy.validation_rules)} rule(s) satisfied",
            )
        return ReadinessCheck(
            "validation_rules", ReadinessCheckOutcome.FAILED,
            "; ".join(problems),
        )

    # ------------------------------------------------------------------ #
    # Internal — persistence
    # ------------------------------------------------------------------ #

    def _persist(
        self,
        result: ReadinessResult,
        version: DatasetVersion,
    ) -> ReadinessEvaluation:
        row = ReadinessEvaluation(
            dataset_version_id=version.id,
            status=result.status,
            checks_json=json.dumps(result.check_dict()),
            reasons_json=json.dumps(result.reasons),
            policy_json=json.dumps(result.policy.to_dict()),
            snapshot_json=json.dumps(
                {
                    "row_count": version.row_count,
                    "schema_hash": version.schema_hash,
                    "checksum": version.checksum,
                }
            ),
            observed_row_count=result.observed_row_count,
        )
        self._session.add(row)
        self._session.flush()
        return row


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _observed_columns(version: DatasetVersion) -> dict[str, str]:
    """Extract {name: dtype} from a dataset version's metadata."""
    raw = version.metadata_json
    if not raw:
        return {}
    try:
        meta = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    columns = meta.get("columns") or []
    out: dict[str, str] = {}
    for c in columns:
        if isinstance(c, dict) and "name" in c and "dtype" in c:
            out[str(c["name"])] = str(c["dtype"])
    return out


def _metadata(version: DatasetVersion) -> dict[str, Any]:
    if not version.metadata_json:
        return {}
    try:
        meta = json.loads(version.metadata_json)
    except (ValueError, TypeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _missing_ratio(version: DatasetVersion) -> float | None:
    meta = _metadata(version)
    if "missing_ratio" in meta:
        try:
            return float(meta["missing_ratio"])
        except (TypeError, ValueError):
            return None
    if "missing_value_count" in meta and version.row_count:
        try:
            count = float(meta["missing_value_count"])
            return count / float(version.row_count)
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    return None
