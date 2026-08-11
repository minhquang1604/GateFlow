"""Training manager for managing training runs.

The manager owns the training-run lifecycle. Orchestrators and trackers must
go through the manager — they must not directly mutate TrainingRun rows.

Public methods:
    create_run(...)
    start_run(...)
    complete_run(...)
    fail_run(...)
    cancel_run(...)
    attach_mlflow_run(...)
    get_run(...)
    list_runs(...)
    update_metadata(...)
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.training_run import RunStatus, TrainingRun, TriggerType
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    InvalidStatusTransitionError,
    TrainingRunNotFoundError,
)
from mlops_framework.training.lifecycle import is_terminal, validate_transition


class TrainingManager:
    """Manages training runs.

    This manager provides business logic for:
    - Creating training runs
    - Driving the run lifecycle (PENDING -> RUNNING -> terminal)
    - Validating status transitions
    - Linking runs to dataset versions
    - Linking runs to MLflow run IDs
    """

    def __init__(
        self,
        session: Session,
        dataset_manager: DatasetManager | None = None,
    ) -> None:
        """Initialize training manager.

        Args:
            session: SQLAlchemy session
            dataset_manager: Optional dataset manager for validation
        """
        self._session = session
        self._dataset_manager = dataset_manager

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _get_dataset_manager(self) -> DatasetManager:
        if self._dataset_manager is None:
            self._dataset_manager = DatasetManager(self._session)
        return self._dataset_manager

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    def _parse_status(self, status: "str | RunStatus") -> RunStatus:
        if isinstance(status, RunStatus):
            return status
        try:
            return RunStatus(status.upper())
        except ValueError as exc:
            raise ValueError(f"Invalid status: {status}") from exc

    # ------------------------------------------------------------------ #
    # Creation
    # ------------------------------------------------------------------ #

    def create_run(
        self,
        dataset_version_id: int,
        trigger_type: "str | TriggerType" = TriggerType.MANUAL,
        pipeline_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TrainingRun:
        """Create a new training run in PENDING state.

        Args:
            dataset_version_id: ID of the dataset version to train on.
            trigger_type: How the run was triggered.
            pipeline_id: Optional orchestrator pipeline/DAG identifier.
            metadata: Optional free-form metadata.

        Returns:
            TrainingRun: Created training run.

        Raises:
            DatasetVersionNotFoundError: If the dataset version does not exist.
        """
        # Validate that dataset version exists
        dataset_manager = self._get_dataset_manager()
        dataset_manager.get_version(dataset_version_id)

        # Parse trigger type
        if isinstance(trigger_type, TriggerType):
            trigger = trigger_type
        else:
            try:
                trigger = TriggerType(trigger_type.upper())
            except ValueError:
                trigger = TriggerType.MANUAL

        run = TrainingRun(
            dataset_version_id=dataset_version_id,
            pipeline_id=pipeline_id,
            status=RunStatus.PENDING,
            trigger_type=trigger,
            metadata_json=json.dumps(metadata) if metadata else None,
        )

        self._session.add(run)
        self._session.flush()

        return run

    # ------------------------------------------------------------------ #
    # Lifecycle transitions
    # ------------------------------------------------------------------ #

    def start_run(self, run_id: int, mlflow_run_id: str | None = None) -> TrainingRun:
        """Transition a run PENDING -> RUNNING.

        Args:
            run_id: Training run ID.
            mlflow_run_id: Optional MLflow run identifier to attach.

        Returns:
            TrainingRun: The updated run.

        Raises:
            TrainingRunNotFoundError: If the run does not exist.
            InvalidStatusTransitionError: If the current status is not PENDING.
        """
        run = self.get_run(run_id)
        if run.status != RunStatus.PENDING:
            raise InvalidStatusTransitionError(
                f"Cannot start run in status {run.status.value}; must be PENDING"
            )
        validate_transition(run.status, RunStatus.RUNNING)

        run.status = RunStatus.RUNNING
        run.started_at = self._now()
        if mlflow_run_id is not None:
            run.mlflow_run_id = mlflow_run_id

        self._session.flush()
        return run

    def complete_run(self, run_id: int) -> TrainingRun:
        """Transition a run RUNNING -> SUCCESS."""
        run = self.get_run(run_id)
        if is_terminal(run.status):
            raise InvalidStatusTransitionError(
                f"Cannot complete run in terminal status {run.status.value}"
            )
        validate_transition(run.status, RunStatus.SUCCESS)

        run.status = RunStatus.SUCCESS
        run.completed_at = self._now()
        self._session.flush()
        return run

    def fail_run(self, run_id: int, error_message: str | None = None) -> TrainingRun:
        """Transition a run RUNNING -> FAILED, recording an error message."""
        run = self.get_run(run_id)
        if is_terminal(run.status):
            raise InvalidStatusTransitionError(
                f"Cannot fail run in terminal status {run.status.value}"
            )
        validate_transition(run.status, RunStatus.FAILED)

        run.status = RunStatus.FAILED
        run.completed_at = self._now()
        if error_message is not None:
            run.error_message = error_message

        self._session.flush()
        return run

    def cancel_run(self, run_id: int) -> TrainingRun:
        """Cancel a PENDING or RUNNING run."""
        run = self.get_run(run_id)
        if is_terminal(run.status):
            raise InvalidStatusTransitionError(
                f"Cannot cancel run in terminal status {run.status.value}"
            )
        validate_transition(run.status, RunStatus.CANCELLED)

        run.status = RunStatus.CANCELLED
        if run.started_at is None:
            run.started_at = self._now()
        run.completed_at = self._now()
        self._session.flush()
        return run

    # ------------------------------------------------------------------ #
    # Misc updates
    # ------------------------------------------------------------------ #

    def attach_mlflow_run(self, run_id: int, mlflow_run_id: str) -> TrainingRun:
        """Attach an MLflow run ID to a training run."""
        run = self.get_run(run_id)
        run.mlflow_run_id = mlflow_run_id
        self._session.flush()
        return run

    def update_metadata(self, run_id: int, metadata: dict[str, Any]) -> TrainingRun:
        """Merge ``metadata`` into the run's stored metadata JSON."""
        run = self.get_run(run_id)
        existing: dict[str, Any] = {}
        if run.metadata_json:
            existing = json.loads(run.metadata_json)
        existing.update(metadata)
        run.metadata_json = json.dumps(existing)
        self._session.flush()
        return run

    # ------------------------------------------------------------------ #
    # Backwards-compatible generic API
    # ------------------------------------------------------------------ #

    def update_status(self, run_id: int, status: str) -> TrainingRun:
        """Generic status update.

        Kept for backwards compatibility. Prefer the typed
        :meth:`start_run` / :meth:`complete_run` / :meth:`fail_run` /
        :meth:`cancel_run` methods in new code.
        """
        new_status = self._parse_status(status)
        if new_status == RunStatus.RUNNING:
            return self.start_run(run_id)
        if new_status == RunStatus.SUCCESS:
            return self.complete_run(run_id)
        if new_status == RunStatus.FAILED:
            return self.fail_run(run_id)
        if new_status == RunStatus.CANCELLED:
            return self.cancel_run(run_id)
        if new_status == RunStatus.PENDING:
            raise InvalidStatusTransitionError(
                "Cannot transition back to PENDING from a created run"
            )
        raise ValueError(f"Unsupported status: {status}")

    def update_run(
        self,
        run_id: int,
        metadata: dict[str, Any] | None = None,
    ) -> TrainingRun:
        """Update metadata for a training run. Backwards-compatible alias."""
        run = self.get_run(run_id)
        if metadata is not None:
            self.update_metadata(run_id, metadata)
            # refresh local reference after flush
            run = self.get_run(run_id)
        return run

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def get_run(self, run_id: int) -> TrainingRun:
        """Get a training run by ID."""
        run = self._session.get(TrainingRun, run_id)
        if run is None:
            raise TrainingRunNotFoundError(f"TrainingRun with id {run_id} not found")
        return run

    def list_runs(self, dataset_version_id: int | None = None) -> list[TrainingRun]:
        """List training runs, optionally filtered by dataset version."""
        query = select(TrainingRun)
        if dataset_version_id is not None:
            query = query.where(TrainingRun.dataset_version_id == dataset_version_id)
        return list(self._session.execute(query).scalars().all())

    def get_run_metadata(self, run_id: int) -> dict[str, Any]:
        """Get parsed metadata for a training run.

        Expires the row's metadata_json first and lets the ``get_run``
        below reload it. The session this manager was built on has
        ``expire_on_commit=False`` (see database/session.py), and this
        run's row may just have been written by a completely different
        session — e.g. an Airflow-side pipeline's POST
        .../training-runs/{id}/finish, on its own connection, while this
        session was blocked polling the orchestrator for the same run.
        Without this, ``self._session.get(TrainingRun, run_id)`` inside
        ``get_run`` returns this session's now-stale cached copy instead
        of ever re-querying — every caller of this method exists
        specifically to observe state written elsewhere, so staleness
        here is never the right default.
        """
        run = self.get_run(run_id)
        self._session.expire(run, ["metadata_json"])
        run = self.get_run(run_id)
        if run.metadata_json:
            return json.loads(run.metadata_json)
        return {}
