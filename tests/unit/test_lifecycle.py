"""Unit tests for the training run lifecycle state machine."""

import pytest

from mlops_framework.database.models.training_run import RunStatus
from mlops_framework.exceptions import InvalidStatusTransitionError
from mlops_framework.training.lifecycle import (
    is_terminal,
    is_valid_transition,
    validate_transition,
)


class TestValidTransitions:
    def test_pending_to_running(self):
        assert is_valid_transition(RunStatus.PENDING, RunStatus.RUNNING) is True

    def test_pending_to_cancelled(self):
        assert is_valid_transition(RunStatus.PENDING, RunStatus.CANCELLED) is True

    def test_running_to_success(self):
        assert is_valid_transition(RunStatus.RUNNING, RunStatus.SUCCESS) is True

    def test_running_to_failed(self):
        assert is_valid_transition(RunStatus.RUNNING, RunStatus.FAILED) is True

    def test_running_to_cancelled(self):
        assert is_valid_transition(RunStatus.RUNNING, RunStatus.CANCELLED) is True


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            (RunStatus.SUCCESS, RunStatus.RUNNING),
            (RunStatus.FAILED, RunStatus.RUNNING),
            (RunStatus.CANCELLED, RunStatus.RUNNING),
            (RunStatus.SUCCESS, RunStatus.FAILED),
            (RunStatus.SUCCESS, RunStatus.CANCELLED),
            (RunStatus.FAILED, RunStatus.SUCCESS),
            (RunStatus.CANCELLED, RunStatus.SUCCESS),
            (RunStatus.RUNNING, RunStatus.PENDING),
            (RunStatus.PENDING, RunStatus.SUCCESS),
            (RunStatus.PENDING, RunStatus.FAILED),
        ],
    )
    def test_invalid_transitions_rejected(self, current, target):
        assert is_valid_transition(current, target) is False
        with pytest.raises(InvalidStatusTransitionError):
            validate_transition(current, target)


class TestTerminalStates:
    @pytest.mark.parametrize(
        "status",
        [RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.CANCELLED],
    )
    def test_terminal_states(self, status):
        assert is_terminal(status) is True

    @pytest.mark.parametrize(
        "status",
        [RunStatus.PENDING, RunStatus.RUNNING],
    )
    def test_non_terminal_states(self, status):
        assert is_terminal(status) is False
