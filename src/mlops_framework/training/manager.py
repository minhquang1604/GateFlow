"""Training manager for managing training runs."""

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.training_run import TrainingRun, RunStatus, TriggerType
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    TrainingRunNotFoundError,
    DatasetVersionNotFoundError,
    InvalidStatusTransitionError,
)


# Define valid status transitions
VALID_STATUS_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.SUCCESS: set(),  # Terminal state
    RunStatus.FAILED: set(),  # Terminal state
    RunStatus.CANCELLED: set(),  # Terminal state
}


class TrainingManager:
    """Manages training runs.

    This manager provides business logic for:
    - Creating training runs
    - Updating run status
    - Validating status transitions
    - Ensuring runs are linked to valid dataset versions
    """

    def __init__(self, session: Session, dataset_manager: Optional[DatasetManager] = None) -> None:
        """Initialize training manager.

        Args:
            session: SQLAlchemy session
            dataset_manager: Optional dataset manager for validation
        """
        self._session = session
        self._dataset_manager = dataset_manager

    def _get_dataset_manager(self) -> DatasetManager:
        """Get or create dataset manager."""
        if self._dataset_manager is None:
            self._dataset_manager = DatasetManager(self._session)
        return self._dataset_manager

    def _validate_status_transition(self, current_status: RunStatus, new_status: RunStatus) -> None:
        """Validate that a status transition is allowed.

        Args:
            current_status: Current status
            new_status: New status to transition to

        Raises:
            InvalidStatusTransitionError: If transition is not allowed
        """
        allowed = VALID_STATUS_TRANSITIONS.get(current_status, set())
        if new_status not in allowed:
            raise InvalidStatusTransitionError(
                f"Invalid status transition from {current_status.value} to {new_status.value}"
            )

    def create_run(
        self,
        dataset_version_id: int,
        trigger_type: str = "MANUAL",
        metadata: Optional[dict[str, Any]] = None,
    ) -> TrainingRun:
        """Create a new training run.

        Args:
            dataset_version_id: ID of the dataset version to train on
            trigger_type: Trigger type (MANUAL, SCHEDULED, DRIFT, API)
            metadata: Optional metadata dictionary

        Returns:
            TrainingRun: Created training run

        Raises:
            DatasetVersionNotFoundError: If dataset version not found
        """
        # Validate that dataset version exists
        dataset_manager = self._get_dataset_manager()
        dataset_version = dataset_manager.get_version(dataset_version_id)

        # Parse trigger type
        try:
            trigger = TriggerType(trigger_type.upper())
        except ValueError:
            trigger = TriggerType.MANUAL

        # Create training run
        run = TrainingRun(
            dataset_version_id=dataset_version_id,
            status=RunStatus.PENDING,
            trigger_type=trigger,
            metadata_json=json.dumps(metadata) if metadata else None,
        )

        self._session.add(run)
        self._session.flush()

        return run

    def get_run(self, run_id: int) -> TrainingRun:
        """Get a training run by ID.

        Args:
            run_id: Training run ID

        Returns:
            TrainingRun: The training run

        Raises:
            TrainingRunNotFoundError: If run not found
        """
        run = self._session.get(TrainingRun, run_id)
        if run is None:
            raise TrainingRunNotFoundError(f"TrainingRun with id {run_id} not found")
        return run

    def list_runs(self, dataset_version_id: Optional[int] = None) -> list[TrainingRun]:
        """List training runs.

        Args:
            dataset_version_id: Optional filter by dataset version

        Returns:
            list[TrainingRun]: List of training runs
        """
        query = select(TrainingRun)
        if dataset_version_id is not None:
            query = query.where(TrainingRun.dataset_version_id == dataset_version_id)
        return list(self._session.execute(query).scalars().all())

    def update_status(
        self,
        run_id: int,
        status: str,
    ) -> TrainingRun:
        """Update the status of a training run.

        Args:
            run_id: Training run ID
            status: New status string

        Returns:
            TrainingRun: Updated training run

        Raises:
            TrainingRunNotFoundError: If run not found
            InvalidStatusTransitionError: If transition is not allowed
        """
        run = self.get_run(run_id)

        # Parse new status
        try:
            new_status = RunStatus(status.upper())
        except ValueError:
            raise ValueError(f"Invalid status: {status}")

        # Validate transition
        self._validate_status_transition(run.status, new_status)

        # Update status
        run.status = new_status

        # Set timestamps based on status
        if new_status == RunStatus.RUNNING and run.started_at is None:
            from datetime import datetime, timezone
            run.started_at = datetime.now(timezone.utc)
        elif new_status in {RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED}:
            from datetime import datetime, timezone
            run.completed_at = datetime.now(timezone.utc)

        self._session.flush()

        return run

    def update_run(
        self,
        run_id: int,
        metadata: Optional[dict[str, Any]] = None,
    ) -> TrainingRun:
        """Update metadata for a training run.

        Args:
            run_id: Training run ID
            metadata: New metadata dictionary

        Returns:
            TrainingRun: Updated training run
        """
        run = self.get_run(run_id)

        if metadata is not None:
            # Merge with existing metadata
            existing = {}
            if run.metadata_json:
                existing = json.loads(run.metadata_json)
            existing.update(metadata)
            run.metadata_json = json.dumps(existing)

        self._session.flush()

        return run

    def get_run_metadata(self, run_id: int) -> dict[str, Any]:
        """Get parsed metadata for a training run.

        Args:
            run_id: Training run ID

        Returns:
            dict[str, Any]: Parsed metadata
        """
        run = self.get_run(run_id)
        if run.metadata_json:
            return json.loads(run.metadata_json)
        return {}
