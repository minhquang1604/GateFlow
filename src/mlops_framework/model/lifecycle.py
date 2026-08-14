"""Model lifecycle state machine.

The framework owns the model version state transitions. The lifecycle is:

    TRAINING -> CANDIDATE -> APPROVED -> PRODUCTION -> ARCHIVED
                            CANDIDATE -> REJECTED
    TRAINING -> REJECTED
    CANDIDATE -> REJECTED
    ARCHIVED  -> APPROVED                              (rollback only)

REJECTED is terminal. ARCHIVED is not: a retired version can be
re-approved and put back into production, which is what
``ModelManager.rollback_to()`` does — see the comment on
``VALID_MODEL_STATE_TRANSITIONS`` for why the edge lands on APPROVED
rather than going straight to PRODUCTION.
"""

from mlops_framework.database.models.model_version import (
    VALID_MODEL_STATE_TRANSITIONS,
    ModelState,
)
from mlops_framework.exceptions import InvalidModelStateTransitionError


def is_valid_transition(current: ModelState, target: ModelState) -> bool:
    return target in VALID_MODEL_STATE_TRANSITIONS.get(current, set())


def validate_transition(current: ModelState, target: ModelState) -> None:
    if not is_valid_transition(current, target):
        raise InvalidModelStateTransitionError(
            f"Invalid model state transition from {current.value} to {target.value}"
        )


def is_terminal(state: ModelState) -> bool:
    return len(VALID_MODEL_STATE_TRANSITIONS.get(state, set())) == 0
