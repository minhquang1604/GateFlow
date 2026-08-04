"""Unit tests for AirflowOrchestrator.

The adapter is tested with a fake ``httpx.Client`` that records
requests and returns canned responses. This keeps the test suite
hermetic — no live Airflow deployment is required.

The fake is deliberately **strict**: any URL the test did not register
returns 404, and every response body must be registered explicitly.
An earlier version answered 200 with ``{}`` to anything it did not
recognise, which is why this suite passed for months while the adapter
addressed DAG runs at ``/api/v1/dagRuns/{id}`` — a route Airflow does
not serve. A fake that says yes to everything tests nothing.

``tests/integration/test_airflow_live.py`` runs the same flows against
a real Airflow and is the backstop for exactly that class of mistake.
"""

import json
from typing import Any, Optional

import pytest

from mlops_framework.exceptions import (
    ExecutionNotFoundError,
    OrchestratorConfigError,
)
from mlops_framework.orchestration.airflow import AirflowOrchestrator
from mlops_framework.orchestration.base import ExecutionState

DAG_ID = "mlops_training_pipeline"


class _Response:
    def __init__(self, status_code: int, payload: Optional[dict[str, Any]] = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeAirflowClient:
    """Implements the httpx.Client surface the adapter uses.

    Unregistered routes 404. That is what a real Airflow does with a
    URL it does not serve, and it is the only way this fake can fail a
    test that targets the wrong endpoint.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, Any]] = []
        self.responses: dict[tuple[str, str], _Response] = {}

    def _route(self, method: str, url: str) -> _Response:
        return self.responses.get(
            (method, url),
            _Response(404, {"detail": f"no route for {method} {url}"}),
        )

    def post(self, url: str, json: Any = None) -> _Response:
        self.calls.append(("POST", url, json))
        return self._route("POST", url)

    def patch(self, url: str, json: Any = None) -> _Response:
        self.calls.append(("PATCH", url, json))
        return self._route("PATCH", url)

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


def _run_body(state: str, dag_run_id: str = "run-1", **extra) -> dict:
    body = {
        "dag_run_id": dag_run_id,
        "dag_id": DAG_ID,
        "state": state,
        "start_date": "2026-01-01T00:00:00+00:00",
        "end_date": None,
        "logical_date": "2026-01-01T00:00:00+00:00",
        "external_trigger": True,
        "conf": {"training_run_id": 42},
    }
    body.update(extra)
    return body


class TestExecutionId:
    """The composite id is the adapter's contract with its callers."""

    def test_make_and_split_round_trip(self):
        eid = AirflowOrchestrator.make_execution_id(DAG_ID, "run-1")
        assert eid == f"{DAG_ID}/run-1"
        assert AirflowOrchestrator._split_execution_id(eid) == (DAG_ID, "run-1")

    @pytest.mark.parametrize("bare", ["run-1", "", "/run-1", "dag/"])
    def test_non_composite_id_is_rejected(self, bare):
        """A bare dag_run_id is not addressable — say so, don't 404.

        Airflow nests every DAG-run route under its DAG and offers no
        lookup by bare run id, so there is no request that could
        succeed.
        """
        orch, client = _make()
        with pytest.raises(ExecutionNotFoundError, match="not addressable"):
            orch.get_execution_status(bare)
        assert client.calls == [], "no HTTP request should be attempted"


class TestTriggerPipeline:
    def test_returns_composite_execution_id(self):
        orch, client = _make({
            ("POST", f"/api/v1/dags/{DAG_ID}/dagRuns"):
                _resp({"dag_run_id": "mlops-abc123", "dag_id": DAG_ID}),
        })
        exec_id = orch.trigger_pipeline(DAG_ID, {"k": 1})
        assert exec_id == f"{DAG_ID}/mlops-abc123"
        # The config is forwarded as the run's conf.
        assert any(c[0] == "POST" and c[2]["conf"] == {"k": 1} for c in client.calls)

    def test_generated_run_id_is_unique_per_call(self):
        """Two triggers must not collide — Airflow 409s on a duplicate id."""
        orch, client = _make({
            ("POST", f"/api/v1/dags/{DAG_ID}/dagRuns"):
                _resp({"dag_run_id": "server-assigned", "dag_id": DAG_ID}),
        })
        orch.trigger_pipeline(DAG_ID, {})
        orch.trigger_pipeline(DAG_ID, {})
        sent = [c[2]["dag_run_id"] for c in client.calls if c[0] == "POST"]
        assert len(sent) == 2 and sent[0] != sent[1]

    def test_missing_dag_raises(self):
        orch, _ = _make({
            ("POST", "/api/v1/dags/missing/dagRuns"):
                _resp({"detail": "DAG not found"}, status=404),
        })
        with pytest.raises(OrchestratorConfigError):
            orch.trigger_pipeline("missing", {})

    def test_duplicate_run_raises_with_conflict_detail(self):
        orch, _ = _make({
            ("POST", f"/api/v1/dags/{DAG_ID}/dagRuns"):
                _resp({"detail": "already exists"}, status=409),
        })
        with pytest.raises(OrchestratorConfigError, match="duplicate"):
            orch.trigger_pipeline(DAG_ID, {})

    def test_unexpected_status_raises(self):
        orch, _ = _make({
            ("POST", "/api/v1/dags/broken/dagRuns"):
                _resp({"detail": "boom"}, status=500),
        })
        with pytest.raises(OrchestratorConfigError):
            orch.trigger_pipeline("broken", {})


class TestGetStatus:
    def test_uses_the_dag_nested_route(self):
        """Guards the exact bug this suite used to miss."""
        orch, client = _make({
            (
                "GET", f"/api/v1/dags/{DAG_ID}/dagRuns/run-1",
            ): _resp(_run_body("running")),
        })
        orch.get_execution_status(f"{DAG_ID}/run-1")
        assert client.calls == [
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/run-1", None)
        ]

    def test_running_state(self):
        orch, _ = _make({
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/run-1"):
                _resp(_run_body("running")),
        })
        status = orch.get_execution_status(f"{DAG_ID}/run-1")
        assert status.state == ExecutionState.RUNNING
        assert status.pipeline_id == DAG_ID
        assert status.execution_id == f"{DAG_ID}/run-1"
        assert status.metadata["conf"] == {"training_run_id": 42}

    def test_success_state(self):
        orch, _ = _make({
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/run-2"):
                _resp(_run_body("success", "run-2", end_date="2026-01-01T00:01:00+00:00")),
        })
        status = orch.get_execution_status(f"{DAG_ID}/run-2")
        assert status.state == ExecutionState.SUCCESS
        assert status.is_terminal

    def test_failed_state(self):
        orch, _ = _make({
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/run-3"):
                _resp(_run_body("failed", "run-3", end_date="2026-01-01T00:01:00+00:00")),
        })
        status = orch.get_execution_status(f"{DAG_ID}/run-3")
        assert status.state == ExecutionState.FAILED

    def test_unknown_execution_raises(self):
        orch, _ = _make({
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/nope"):
                _resp({"detail": "not found"}, status=404),
        })
        with pytest.raises(ExecutionNotFoundError):
            orch.get_execution_status(f"{DAG_ID}/nope")

    def test_ids_needing_escaping_are_quoted(self):
        """Airflow allows ':' and '+' in a dag_run_id; the path must escape."""
        rid = "mlops-2026-01-01T00:00:00+00:00"
        orch, client = _make({
            (
                "GET",
                f"/api/v1/dags/{DAG_ID}/dagRuns/"
                "mlops-2026-01-01T00%3A00%3A00%2B00%3A00",
            ): _resp(_run_body("running", rid)),
        })
        status = orch.get_execution_status(f"{DAG_ID}/{rid}")
        assert status.state == ExecutionState.RUNNING


class TestCancel:
    def test_cancel_patches_state_to_failed(self):
        """Cancel must not DELETE — that erases the run from Airflow."""
        orch, client = _make({
            ("PATCH", f"/api/v1/dags/{DAG_ID}/dagRuns/run-x"):
                _resp(_run_body("failed", "run-x", end_date="2026-01-01T00:02:00+00:00")),
        })
        status = orch.cancel_execution(f"{DAG_ID}/run-x")
        assert status.state == ExecutionState.CANCELLED
        assert client.calls == [
            ("PATCH", f"/api/v1/dags/{DAG_ID}/dagRuns/run-x", {"state": "failed"})
        ]
        assert not any(c[0] == "DELETE" for c in client.calls)

    def test_cancel_unknown_raises(self):
        orch, _ = _make({
            ("PATCH", f"/api/v1/dags/{DAG_ID}/dagRuns/nope"):
                _resp({"detail": "not found"}, status=404),
        })
        with pytest.raises(ExecutionNotFoundError):
            orch.cancel_execution(f"{DAG_ID}/nope")


class TestConfiguration:
    def test_missing_base_url_raises(self):
        with pytest.raises(OrchestratorConfigError):
            AirflowOrchestrator(base_url="")

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
        orch, _ = _make({
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/run-1/taskInstances"): _resp(body),
        })
        states = orch.get_task_instance_states(f"{DAG_ID}/run-1")
        assert states == {"resolve_context": "success", "train": "running"}

    def test_returns_empty_dict_on_404(self):
        orch, _ = _make({
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/missing/taskInstances"):
                _resp({"detail": "not found"}, status=404),
        })
        assert orch.get_task_instance_states(f"{DAG_ID}/missing") == {}

    def test_returns_empty_dict_on_500(self):
        orch, _ = _make({
            ("GET", f"/api/v1/dags/{DAG_ID}/dagRuns/broken/taskInstances"):
                _resp({}, status=500),
        })
        assert orch.get_task_instance_states(f"{DAG_ID}/broken") == {}

    def test_bare_id_returns_empty_rather_than_raising(self):
        """This query is documented as best-effort and never raising."""
        orch, _ = _make()
        assert orch.get_task_instance_states("run-1") == {}
