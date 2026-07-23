"""Unit tests for LocalDockerOrchestrator."""

import time

import pytest

from mlops_framework.exceptions import ExecutionNotFoundError
from mlops_framework.orchestration.base import ExecutionState
from mlops_framework.orchestration.local import LocalDockerOrchestrator


PIPELINE_SUCCESS = "tests._pipelines.pipelines:success"
PIPELINE_FAIL = "tests._pipelines.pipelines:fail"
PIPELINE_SLOW = "tests._pipelines.pipelines:slow"
PIPELINE_RAISES = "tests._pipelines.pipelines:raises"


def _wait_for_terminal(orch: LocalDockerOrchestrator, exec_id: str, timeout: float = 5.0):
    """Poll until the execution leaves RUNNING/PENDING or the timeout fires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = orch.get_execution_status(exec_id)
        if status.is_terminal or status.state in {ExecutionState.SUCCESS, ExecutionState.FAILED}:
            return status
        time.sleep(0.05)
    return orch.get_execution_status(exec_id)


class TestSuccessfulExecution:
    def test_trigger_pipeline_returns_execution_id(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_SUCCESS, {"x": 1})
            assert isinstance(exec_id, str) and exec_id
        finally:
            orch.shutdown()

    def test_status_becomes_success(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_SUCCESS, {"x": 1})
            status = _wait_for_terminal(orch, exec_id)
            assert status.state == ExecutionState.SUCCESS
            assert status.exit_code == 0
        finally:
            orch.shutdown()

    def test_captures_metadata_from_pipeline(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_SUCCESS, {"alpha": 1, "beta": 2})
            status = _wait_for_terminal(orch, exec_id)
            assert status.metadata.get("status") == "SUCCESS"
            assert status.metadata.get("config_keys") == ["alpha", "beta"]
        finally:
            orch.shutdown()


class TestFailedExecution:
    def test_status_becomes_failed_on_exit_code(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_FAIL, {})
            status = _wait_for_terminal(orch, exec_id)
            assert status.state == ExecutionState.FAILED
            assert status.exit_code == 2
            assert status.message and "intentional failure" in status.message
        finally:
            orch.shutdown()

    def test_status_becomes_failed_on_exception(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_RAISES, {})
            status = _wait_for_terminal(orch, exec_id)
            assert status.state == ExecutionState.FAILED
            assert status.exit_code != 0
        finally:
            orch.shutdown()


class TestUnknownExecution:
    def test_unknown_execution_raises(self):
        orch = LocalDockerOrchestrator()
        with pytest.raises(ExecutionNotFoundError):
            orch.get_execution_status("does-not-exist")

    def test_cancel_unknown_execution_raises(self):
        orch = LocalDockerOrchestrator()
        with pytest.raises(ExecutionNotFoundError):
            orch.cancel_execution("does-not-exist")


class TestStatusRetrieval:
    def test_running_state_observed(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_SLOW, {})
            status = orch.get_execution_status(exec_id)
            # The pipeline sleeps 2s, so within a short window it should
            # still be RUNNING or already SUCCESS.
            assert status.state in {ExecutionState.RUNNING, ExecutionState.SUCCESS}
        finally:
            orch.shutdown()

    def test_invalid_pipeline_id_reported_as_failed(self):
        """A syntactically valid but importable-nowhere pipeline id should
        surface as a FAILED execution with a meaningful message."""
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(
                "this_does_not_exist_anywhere:main", {}
            )
            status = _wait_for_terminal(orch, exec_id)
            assert status.state == ExecutionState.FAILED
            assert status.exit_code != 0
        finally:
            orch.shutdown()


class TestCancellation:
    def test_cancel_running_execution(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_SLOW, {})
            # Give the subprocess a moment to start.
            time.sleep(0.2)
            status = orch.cancel_execution(exec_id)
            assert status.state == ExecutionState.CANCELLED
        finally:
            orch.shutdown()

    def test_cancel_after_completion_returns_terminal_state(self):
        orch = LocalDockerOrchestrator()
        try:
            exec_id = orch.trigger_pipeline(PIPELINE_SUCCESS, {})
            _wait_for_terminal(orch, exec_id)
            status = orch.cancel_execution(exec_id)
            assert status.state == ExecutionState.SUCCESS
        finally:
            orch.shutdown()
