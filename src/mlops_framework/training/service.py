"""Application-level TrainingService.

This service is the framework's facade for "run a training pipeline".
It composes:

    * TrainingManager  (owns the lifecycle in the database)
    * Orchestrator     (executes the pipeline; injected)
    * ExperimentTracker (records params/metrics; injected)

The service never imports an Airflow or MLflow SDK directly — it depends
only on the framework's abstractions. Adapters are injected at
construction time.

    TrainingService(orchestrator=LocalDockerOrchestrator(),
                   tracker=MLflowTracker())
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from mlops_framework.orchestration.base import ExecutionState, Orchestrator
from mlops_framework.training.manager import TrainingManager
from mlops_framework.tracking.base import ExperimentTracker


class TrainingService:
    """High-level training service.

    Typical usage:

        service = TrainingService(
            training_manager=training_manager,
            orchestrator=LocalDockerOrchestrator(),
            tracker=MLflowTracker(),
        )
        run = service.create_run(dataset_version_id=42, pipeline_id="...")
        execution_id = service.start_run(run.id)
        service.poll_until_done(run.id)
        service.complete_run(run.id, metrics={"f1": 0.9})
    """

    def __init__(
        self,
        training_manager: TrainingManager,
        orchestrator: Orchestrator,
        tracker: Optional[ExperimentTracker] = None,
    ) -> None:
        self._manager = training_manager
        self._orchestrator = orchestrator
        self._tracker = tracker

    # ------------------------------------------------------------------ #
    # Run creation
    # ------------------------------------------------------------------ #

    def create_run(
        self,
        dataset_version_id: int,
        pipeline_id: str,
        trigger_type: str = "MANUAL",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Create a PENDING training run for a dataset version."""
        return self._manager.create_run(
            dataset_version_id=dataset_version_id,
            pipeline_id=pipeline_id,
            trigger_type=trigger_type,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start_run(self, run_id: int) -> str:
        """Start the run.

        Begins a tracker run (if a tracker is configured), attaches the
        tracker run id, triggers the pipeline through the orchestrator,
        and transitions the run to RUNNING.

        Returns:
            The orchestrator's execution id.
        """
        run = self._manager.get_run(run_id)

        tracker_run_id: Optional[str] = None
        if self._tracker is not None:
            tracker_run_id = self._tracker.start_run(
                run_name=f"training-run-{run_id}",
                tags={"training_run_id": str(run_id), "pipeline_id": run.pipeline_id or ""},
            )

        execution_id = self._orchestrator.trigger_pipeline(
            pipeline_id=run.pipeline_id or "",
            config={
                "training_run_id": run.id,
                "dataset_version_id": run.dataset_version_id,
            },
        )
        self._manager.update_metadata(
            run_id, {"orchestrator_execution_id": execution_id}
        )
        self._manager.start_run(run_id, mlflow_run_id=tracker_run_id)
        return execution_id

    def sync_from_orchestrator(self, run_id: int) -> str:
        """Reconcile the run's status with the orchestrator.

        Returns the orchestrator's current state.
        """
        run = self._manager.get_run(run_id)
        meta = self._manager.get_run_metadata(run_id)
        execution_id = meta.get("orchestrator_execution_id")
        if not execution_id:
            return "UNKNOWN"
        status = self._orchestrator.get_execution_status(execution_id)
        if status.state == ExecutionState.FAILED and run.status.value == "RUNNING":
            self._manager.fail_run(run_id, error_message=status.message)
        elif status.state == ExecutionState.SUCCESS and run.status.value == "RUNNING":
            self._manager.complete_run(run_id)
        return status.state.value

    def complete_run(self, run_id: int) -> Any:
        """Mark the run SUCCESS and end the tracker run (if any)."""
        run = self._manager.get_run(run_id)
        if self._tracker is not None and run.mlflow_run_id:
            self._tracker.end_run(status="SUCCESS")
        return self._manager.complete_run(run_id)

    def fail_run(self, run_id: int, error_message: Optional[str] = None) -> Any:
        """Mark the run FAILED and end the tracker run (if any)."""
        run = self._manager.get_run(run_id)
        if self._tracker is not None and run.mlflow_run_id:
            self._tracker.end_run(status="FAILED")
        return self._manager.fail_run(run_id, error_message=error_message)

    def cancel_run(self, run_id: int) -> Any:
        """Cancel the run via the orchestrator and the lifecycle."""
        run = self._manager.get_run(run_id)
        meta = self._manager.get_run_metadata(run_id)
        execution_id = meta.get("orchestrator_execution_id")
        if execution_id:
            self._orchestrator.cancel_execution(execution_id)
        if self._tracker is not None and run.mlflow_run_id:
            self._tracker.end_run(status="CANCELLED")
        return self._manager.cancel_run(run_id)

    # ------------------------------------------------------------------ #
    # Polling helpers
    # ------------------------------------------------------------------ #

    def wait_for_completion(
        self,
        run_id: int,
        timeout: float = 60.0,
        poll_interval: float = 0.2,
    ) -> str:
        """Poll the orchestrator until the execution is terminal, then
        sync the run status.

        Returns the final orchestrator state value.
        """
        import time
        meta = self._manager.get_run_metadata(run_id)
        execution_id = meta.get("orchestrator_execution_id")
        if not execution_id:
            return "UNKNOWN"
        deadline = time.time() + timeout
        last_state = "UNKNOWN"
        while time.time() < deadline:
            status = self._orchestrator.get_execution_status(execution_id)
            last_state = status.state.value
            if status.is_terminal:
                break
            time.sleep(poll_interval)
        # Sync DB state with orchestrator.
        if last_state == ExecutionState.SUCCESS.value:
            self.complete_run(run_id)
        elif last_state == ExecutionState.FAILED.value:
            self.fail_run(run_id, error_message=self._last_message(execution_id))
        elif last_state == ExecutionState.CANCELLED.value:
            self.cancel_run(run_id)
        return last_state

    def _last_message(self, execution_id: str) -> Optional[str]:
        try:
            return self._orchestrator.get_execution_status(execution_id).message
        except Exception:
            return None
