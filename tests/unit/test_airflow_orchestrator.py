"""Unit tests for AirflowOrchestrator.

The adapter is tested with a fake ``httpx.Client`` that records
requests and returns canned responses. This keeps the test suite
hermetic — no live Airflow deployment is required.
"""

import json
from typing import Any, Optional

import httpx
import pytest

from mlops_framework.exceptions import (
    ExecutionNotFoundError,
    OrchestratorConfigError,
)
from mlops_framework.orchestration.airflow import AirflowOrchestrator
from mlops_framework.orchestration.base import ExecutionState


class _Response:
    def __init__(self, status_code: int, payload: Optional[dict[str, Any]] = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAirflowClient:
    """Implements the httpx.Client surface the adapter uses."""

    def __init__(self):
        self.calls: list[tuple[str, str, Any]] = []
        self.responses: dict[tuple[str, str], _Response] = {}
        self._default_response = _Response(200, {})

    def _route(self, method: str, url: str) -> _Response:
        return self.responses.get((method, url), self._default_response)

    def post(self, url: str, json: Any = None) -> _Response:
        self.calls.append(("POST", url, json))
        return self._route("POST", url)

    def get(self, url: str) -> _Response:
        self.calls.append(("GET", url, None))
        return self._route("GET", url)

    def delete(self, url: str) -> _Response:
        self.calls.append(("DELETE", url, None))
        return self._route("DELETE", url)

    def close(self) -> None:
        pass


def _make(responses: Optional[dict] = None) -> tuple[AirflowOrchestrator, _FakeAirflowClient]:
    """Build an orchestrator with a fake HTTP client.

    ``responses`` maps (method, url) -> {status, body}.
    """
    client = _FakeAirflowClient()
    for (method, url), cfg in (responses or {}).items():
        client.responses[(method, url)] = _Response(
            cfg.get("status", 200), cfg.get("body", {})
        )
    orch = AirflowOrchestrator(
        base_url="http://airflow.local:8080",
        http_client=client,  # type: ignore[arg-type]
    )
    return orch, client


def _resp(body: dict, status: int = 200) -> dict:
    return {"status": status, "body": body}


class TestTriggerPipeline:
    def test_returns_dag_run_id(self):
        orch, client = _make({
            ("POST", "/api/v1/dags/mlops_training_pipeline/dagRuns"):
                _resp({"dag_run_id": "mlops-2026-01-01T00:00:00+00:00"}),
        })
        exec_id = orch.trigger_pipeline("mlops_training_pipeline", {"k": 1})
        assert exec_id == "mlops-2026-01-01T00:00:00+00:00"
        # The config is forwarded as the run's conf.
        assert any(c[0] == "POST" and c[2]["conf"] == {"k": 1} for c in client.calls)

    def test_missing_dag_raises(self):
        orch, _ = _make({
            ("POST", "/api/v1/dags/missing/dagRuns"):
                _resp({"detail": "DAG not found"}, status=404),
        })
        with pytest.raises(OrchestratorConfigError):
            orch.trigger_pipeline("missing", {})

    def test_unexpected_status_raises(self):
        orch, _ = _make({
            ("POST", "/api/v1/dags/broken/dagRuns"):
                _resp({"detail": "boom"}, status=500),
        })
        with pytest.raises(OrchestratorConfigError):
            orch.trigger_pipeline("broken", {})


class TestGetStatus:
    def _run_body(self, state: str) -> dict:
        return {
            "dag_run_id": "run-1",
            "dag_id": "mlops_training_pipeline",
            "state": state,
            "start_date": "2026-01-01T00:00:00+00:00",
            "end_date": None,
            "logical_date": "2026-01-01T00:00:00+00:00",
            "external_trigger": True,
            "conf": {"training_run_id": 42},
        }

    def test_running_state(self):
        orch, _ = _make({
            ("GET", "/api/v1/dagRuns/run-1"): _resp(self._run_body("running")),
        })
        status = orch.get_execution_status("run-1")
        assert status.state == ExecutionState.RUNNING
        assert status.pipeline_id == "mlops_training_pipeline"
        assert status.metadata["conf"] == {"training_run_id": 42}

    def test_success_state(self):
        body = self._run_body("success")
        body["end_date"] = "2026-01-01T00:01:00+00:00"
        orch, _ = _make({("GET", "/api/v1/dagRuns/run-2"): _resp(body)})
        status = orch.get_execution_status("run-2")
        assert status.state == ExecutionState.SUCCESS
        assert status.is_terminal

    def test_failed_state(self):
        body = self._run_body("failed")
        body["end_date"] = "2026-01-01T00:01:00+00:00"
        orch, _ = _make({("GET", "/api/v1/dagRuns/run-3"): _resp(body)})
        status = orch.get_execution_status("run-3")
        assert status.state == ExecutionState.FAILED

    def test_unknown_execution_raises(self):
        orch, _ = _make({
            ("GET", "/api/v1/dagRuns/nope"): _resp({"detail": "not found"}, status=404),
        })
        with pytest.raises(ExecutionNotFoundError):
            orch.get_execution_status("nope")


class TestCancel:
    def test_cancel_returns_cancelled(self):
        orch, _ = _make({("DELETE", "/api/v1/dagRuns/run-x"): _resp({})})
        status = orch.cancel_execution("run-x")
        assert status.state == ExecutionState.CANCELLED
        assert status.message and "deleted" in status.message

    def test_cancel_unknown_raises(self):
        orch, _ = _make({
            ("DELETE", "/api/v1/dagRuns/nope"): _resp({"detail": "not found"}, status=404),
        })
        with pytest.raises(ExecutionNotFoundError):
            orch.cancel_execution("nope")


class TestConfiguration:
    def test_missing_base_url_raises(self):
        with pytest.raises(OrchestratorConfigError):
            AirflowOrchestrator(base_url="")

    def test_uses_default_response(self):
        orch, client = _make()
        client._default_response = _Response(200, {"dag_run_id": "x"})
        exec_id = orch.trigger_pipeline("any", {})
        assert exec_id == "x"

    def test_uses_env_var_when_no_constructor_args(self, monkeypatch):
        """When AIRFLOW_BASE_URL is set, the adapter should pick it up."""
        from mlops_framework.config.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("AIRFLOW_BASE_URL", "http://from-env:8080")
        client = _FakeAirflowClient()
        orch = AirflowOrchestrator(http_client=client)  # type: ignore[arg-type]
        assert orch._base_url == "http://from-env:8080"

    def test_explicit_base_url_overrides_env(self, monkeypatch):
        from mlops_framework.config.settings import get_settings

        get_settings.cache_clear()
        monkeypatch.setenv("AIRFLOW_BASE_URL", "http://from-env:8080")
        client = _FakeAirflowClient()
        orch = AirflowOrchestrator(
            base_url="http://explicit:9090",
            http_client=client,  # type: ignore[arg-type]
        )
        assert orch._base_url == "http://explicit:9090"


class TestTaskInstanceStates:
    def test_returns_state_map_on_200(self):
        body = {
            "task_instances": [
                {"task_id": "resolve_context", "state": "success"},
                {"task_id": "train", "state": "running"},
            ]
        }
        orch, _ = _make({("GET", "/api/v1/dagRuns/run-1/taskInstances"): _resp(body)})
        states = orch.get_task_instance_states("run-1")
        assert states == {"resolve_context": "success", "train": "running"}

    def test_returns_empty_dict_on_404(self):
        orch, _ = _make({
            ("GET", "/api/v1/dagRuns/missing/taskInstances"):
                _resp({"detail": "not found"}, status=404),
        })
        assert orch.get_task_instance_states("missing") == {}

    def test_returns_empty_dict_on_500(self):
        orch, _ = _make({
            ("GET", "/api/v1/dagRuns/broken/taskInstances"): _resp({}, status=500),
        })
        assert orch.get_task_instance_states("broken") == {}
