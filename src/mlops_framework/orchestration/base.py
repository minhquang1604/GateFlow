"""Orchestrator abstraction and shared types.

The framework depends on this ABC; concrete adapters
(LocalDockerOrchestrator, AirflowOrchestrator, ...) live in their own
modules so the framework never imports Airflow/Mlflow/Docker code.

The orchestrator is intentionally minimal:

    trigger_pipeline(pipeline_id, config) -> execution_id
    get_execution_status(execution_id)    -> ExecutionStatus
    cancel_execution(execution_id)        -> ExecutionStatus

It is the framework's responsibility to update TrainingRun rows; the
orchestrator only knows how to run a pipeline somewhere.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class ExecutionState(str, enum.Enum):
    """Lifecycle states an orchestrator reports for an execution."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass
class ExecutionStatus:
    """Snapshot of an orchestrator execution."""

    execution_id: str
    state: ExecutionState
    pipeline_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            ExecutionState.SUCCESS,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "state": self.state.value,
            "pipeline_id": self.pipeline_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "exit_code": self.exit_code,
            "message": self.message,
            "metadata": self.metadata,
            "is_terminal": self.is_terminal,
        }


class Orchestrator(ABC):
    """Abstract orchestrator interface.

    The framework depends on this interface; infrastructure adapters
    (local, Airflow, etc.) implement it. Adapters must be safe to
    instantiate without live infrastructure — they should defer any
    connection until ``trigger_pipeline`` is called.
    """

    @abstractmethod
    def trigger_pipeline(
        self,
        pipeline_id: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        """Trigger a pipeline execution and return an execution identifier.

        Args:
            pipeline_id: Identifier of the pipeline/DAG to trigger.
            config: Free-form configuration forwarded to the pipeline.

        Returns:
            The execution identifier (e.g. DAG run id).
        """
        raise NotImplementedError

    @abstractmethod
    def get_execution_status(self, execution_id: str) -> ExecutionStatus:
        """Return the current status of an execution."""
        raise NotImplementedError

    @abstractmethod
    def cancel_execution(self, execution_id: str) -> ExecutionStatus:
        """Request cancellation of a running execution."""
        raise NotImplementedError
