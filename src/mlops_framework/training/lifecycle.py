"""Training run lifecycle state machine.

The framework owns the lifecycle. Orchestrators and tracking adapters must
go through TrainingManager — they must not directly mutate TrainingRun
records.

Allowed transitions (see database.models.training_run.VALID_STATUS_TRANSITIONS):

    PENDING   -> RUNNING
    PENDING   -> CANCELLED
    RUNNING   -> SUCCESS
    RUNNING   -> FAILED
    RUNNING   -> CANCELLED

SUCCESS, FAILED and CANCELLED are terminal.
"""

from mlops_framework.database.models.training_run import (
    VALID_STATUS_TRANSITIONS,
    RunStatus,
)
from mlops_framework.exceptions import InvalidStatusTransitionError


def is_valid_transition(current: RunStatus, target: RunStatus) -> bool:
    """Return True if a transition from ``current`` to ``target`` is allowed."""
    return target in VALID_STATUS_TRANSITIONS.get(current, set())


def validate_transition(current: RunStatus, target: RunStatus) -> None:
    """Validate a status transition.

    Raises:
        InvalidStatusTransitionError: if the transition is not allowed.
    """
    if not is_valid_transition(current, target):
        raise InvalidStatusTransitionError(
            f"Invalid status transition from {current.value} to {target.value}"
        )


def is_terminal(status: RunStatus) -> bool:
    """Return True if the status is terminal (no further transitions)."""
    return len(VALID_STATUS_TRANSITIONS.get(status, set())) == 0
