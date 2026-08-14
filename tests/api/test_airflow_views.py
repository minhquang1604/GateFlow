"""Tests for the Airflow-backed views.

Airflow is faked rather than run — these tests pin the framework's own
logic (execution-id resolution, payload shaping, degradation), not
Airflow's behaviour. The endpoints were exercised by hand against the
real deployed Airflow before this file was written; what is pinned here
is what a fake can prove deterministically.

Follows the ``_FakeAirflowClient`` pattern in
``tests/unit/test_airflow_orchestrator.py`` at the HTTP layer, and the
``fake_mlflow`` fixture pattern in ``test_mlflow_views.py`` at the
gateway layer — this fakes ``AirflowOrchestrator`` itself, one level
above the HTTP client, since these tests are about the routers, not the
adapter (which already has its own suite).
"""

from __future__ import annotations

from datetime import UTC

import pytest

from mlops_framework.api import airflow_gateway
from mlops_framework.api.routers import airflow_views
from mlops_framework.database.models.training_run import RunStatus, TrainingRun

EXECUTION_ID = "mlops_training_pipeline/mlops-abc123"


class _FakeOrchestrator:
    """Enough of AirflowOrchestrator for these endpoints, and nothing more."""

    def __init__(self):
        self.closed = False
        self.log_calls: list[tuple[str, str, int]] = []

    def close(self) -> None:
        self.closed = True

    def get_health(self):
        return {"scheduler": {"status": "healthy"}}

    def get_import_errors(self):
        return [{"filename": "bad_dag.py", "stack_trace": "SyntaxError", "timestamp": "t"}]

    def get_pools(self):
        return [{"name": "default_pool", "slots": 128, "open_slots": 100}]

    def list_dags(self):
        return [{"dag_id": "mlops_training_pipeline", "is_paused": False}]

    def get_dag_tasks(self, dag_id):
        assert dag_id == "mlops_training_pipeline"
        return [{"task_id": "train", "downstream_task_ids": ["promote"]}]

    def list_dag_runs(self, dag_id, limit=25):
        assert dag_id == "mlops_training_pipeline"
        return [{"dag_run_id": "mlops-abc123", "state": "success", "conf": {"max_depth": 6}}]

    def make_execution_id(self, dag_id, dag_run_id):
        return f"{dag_id}/{dag_run_id}"

    def get_task_instances(self, execution_id):
        assert execution_id == EXECUTION_ID
        return [{"task_id": "train", "state": "success", "try_number": 1}]

    def get_execution_status(self, execution_id):
        from datetime import datetime

        from mlops_framework.orchestration.base import ExecutionState, ExecutionStatus

        assert execution_id == EXECUTION_ID
        return ExecutionStatus(
            execution_id=execution_id,
            state=ExecutionState.SUCCESS,
            pipeline_id="mlops_training_pipeline",
            started_at=datetime(2026, 8, 5, tzinfo=UTC),
            finished_at=datetime(2026, 8, 5, 0, 1, tzinfo=UTC),
            metadata={"conf": {"max_depth": 6}},
        )

    def get_task_log(self, execution_id, task_id, try_number=1):
        self.log_calls.append((execution_id, task_id, try_number))
        if task_id == "missing":
            from mlops_framework.exceptions import ExecutionNotFoundError

            raise ExecutionNotFoundError("no such log")
        return f"log for {task_id} attempt {try_number}"

    def clear_task(self, execution_id, task_id):
        assert execution_id == EXECUTION_ID
        if task_id == "missing":
            from mlops_framework.exceptions import ExecutionNotFoundError

            raise ExecutionNotFoundError("no such task")
        return {"task_instances": [{"task_id": task_id, "state": None}]}

    def retry_task(self, execution_id, task_id):
        return self.clear_task(execution_id, task_id)


@pytest.fixture()
def fake_airflow(monkeypatch):
    """Point both the gateway and the router at a fake orchestrator.

    Patched in two places for the same reason ``test_mlflow_views.py``
    patches both ``mlflow_gateway`` and ``mlflow_views``: ``panel()``
    resolves ``client_or_reason`` in the gateway's own module globals, but
    ``get_task_log`` calls ``client_or_reason()`` directly and holds a
    reference bound by ``from ... import`` at module load, in
    ``airflow_views``'s own namespace.
    """
    fake = _FakeOrchestrator()
    for module in (airflow_gateway, airflow_views):
        monkeypatch.setattr(module, "client_or_reason", lambda: (fake, None))
    return fake


def _make_run(session_factory, execution_id):
    """Insert a TrainingRun (plus the dataset version it requires) whose
    metadata carries the given orchestrator execution id."""
    import json

    from mlops_framework.database.models.dataset import Dataset
    from mlops_framework.database.models.dataset_version import DatasetVersion

    s = session_factory()
    try:
        ds = Dataset(name=f"d-{execution_id or 'none'}")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id,
            version_number=1,
            storage_uri="s3://x",
            checksum="a" * 64,
            schema_hash="b" * 64,
            row_count=10,
        )
        s.add(dv)
        s.flush()
        metadata = {"orchestrator_execution_id": execution_id} if execution_id else {}
        run = TrainingRun(
            dataset_version_id=dv.id,
            pipeline_id="p",
            status=RunStatus.SUCCESS.value,
            metadata_json=json.dumps(metadata),
        )
        s.add(run)
        s.commit()
        return run.id
    finally:
        s.close()


@pytest.fixture()
def run_on_airflow(session_factory):
    return _make_run(session_factory, EXECUTION_ID)


# ---------------------------------------------------------------------- #
# Degradation
# ---------------------------------------------------------------------- #


class TestDegradesWithoutAirflow:
    def test_health_unconfigured(self, client, monkeypatch):
        monkeypatch.setattr(airflow_gateway, "base_url", lambda: None)
        monkeypatch.setattr(
            airflow_gateway,
            "client_or_reason",
            lambda: (None, "AIRFLOW_BASE_URL is not configured"),
        )
        body = client.get("/api/airflow/health").json()
        assert body["available"] is False
        assert "AIRFLOW_BASE_URL" in body["reason"]

    def test_panel_swallows_orchestrator_errors(self, monkeypatch):
        fake = _FakeOrchestrator()
        monkeypatch.setattr(airflow_gateway, "client_or_reason", lambda: (fake, None))

        def explode(_o):
            raise RuntimeError("connection refused")

        result = airflow_gateway.panel(explode)
        assert result.available is False
        assert "connection refused" in result.reason
        assert fake.closed is True  # closed even when the query raises

    def test_tasks_run_without_execution_id(self, client, session_factory):
        run_id = _make_run(session_factory, None)
        body = client.get(f"/api/training-runs/{run_id}/tasks").json()
        assert body["available"] is False

    def test_tasks_run_on_local_orchestrator(self, client, session_factory):
        run_id = _make_run(session_factory, "not-a-composite-id")
        body = client.get(f"/api/training-runs/{run_id}/tasks").json()
        assert body["available"] is False
        assert "local orchestrator" in body["reason"]

    def test_log_returns_503_when_unconfigured(
        self, client, run_on_airflow, monkeypatch
    ):
        monkeypatch.setattr(
            airflow_views, "client_or_reason", lambda: (None, "not configured")
        )
        r = client.get(f"/api/training-runs/{run_on_airflow}/tasks/train/log")
        assert r.status_code == 503


# ---------------------------------------------------------------------- #
# Health / DAGs
# ---------------------------------------------------------------------- #


class TestAirflowHealth:
    def test_bundles_health_import_errors_pools(self, client, fake_airflow):
        data = client.get("/api/airflow/health").json()["data"]
        assert data["health"]["scheduler"]["status"] == "healthy"
        assert data["import_errors"][0]["filename"] == "bad_dag.py"
        assert data["pools"][0]["name"] == "default_pool"


class TestDags:
    def test_list(self, client, fake_airflow):
        data = client.get("/api/airflow/dags").json()["data"]
        assert data["dags"] == [{"dag_id": "mlops_training_pipeline", "is_paused": False}]

    def test_detail_has_tasks_and_runs(self, client, fake_airflow):
        data = client.get("/api/airflow/dags/mlops_training_pipeline").json()["data"]
        assert data["tasks"][0]["downstream_task_ids"] == ["promote"]
        assert data["dag_runs"][0]["conf"] == {"max_depth": 6}

    def test_detail_expands_recent_runs_into_a_task_grid(self, client, fake_airflow):
        """Default grid_runs expands every returned dag_run into its full
        per-task states, using the same execution id the per-run task view
        already resolves — no new orchestrator method involved."""
        data = client.get("/api/airflow/dags/mlops_training_pipeline").json()["data"]
        assert data["grid_run_ids"] == ["mlops-abc123"]
        assert data["grid_cells"] == [
            {"dag_run_id": "mlops-abc123", "task_id": "train", "state": "success", "try_number": 1}
        ]

    def test_grid_runs_zero_skips_the_grid_entirely(self, client, fake_airflow):
        data = client.get(
            "/api/airflow/dags/mlops_training_pipeline", params={"grid_runs": 0}
        ).json()["data"]
        assert data["grid_run_ids"] == []
        assert data["grid_cells"] == []


# ---------------------------------------------------------------------- #
# Per-run tasks (runs.py, via the Airflow gateway)
# ---------------------------------------------------------------------- #


class TestRunTasks:
    def test_includes_dag_run_and_full_task_fields(
        self, client, fake_airflow, run_on_airflow
    ):
        body = client.get(f"/api/training-runs/{run_on_airflow}/tasks").json()
        assert body["available"] is True
        data = body["data"]
        assert data["dag_run"]["state"] == "SUCCESS"
        assert data["dag_run"]["conf"] == {"max_depth": 6}
        assert data["tasks"][0]["try_number"] == 1

    def test_unknown_run_is_404(self, client, fake_airflow):
        assert client.get("/api/training-runs/9999/tasks").status_code == 404


# ---------------------------------------------------------------------- #
# Task log
# ---------------------------------------------------------------------- #


class TestTaskLog:
    def test_returns_log_text(self, client, fake_airflow, run_on_airflow):
        r = client.get(f"/api/training-runs/{run_on_airflow}/tasks/train/log")
        assert r.status_code == 200
        assert r.text == "log for train attempt 1"
        assert fake_airflow.log_calls == [(EXECUTION_ID, "train", 1)]

    def test_try_number_is_forwarded(self, client, fake_airflow, run_on_airflow):
        r = client.get(
            f"/api/training-runs/{run_on_airflow}/tasks/train/log",
            params={"try_number": 3},
        )
        assert r.status_code == 200
        assert fake_airflow.log_calls[-1] == (EXECUTION_ID, "train", 3)

    def test_missing_log_is_404(self, client, fake_airflow, run_on_airflow):
        r = client.get(f"/api/training-runs/{run_on_airflow}/tasks/missing/log")
        assert r.status_code == 404

    def test_non_airflow_run_is_409(self, client, fake_airflow, session_factory):
        run_id = _make_run(session_factory, "not-a-composite-id")
        r = client.get(f"/api/training-runs/{run_id}/tasks/train/log")
        assert r.status_code == 409

    def test_unknown_run_is_404(self, client, fake_airflow):
        r = client.get("/api/training-runs/9999/tasks/train/log")
        assert r.status_code == 404


# ---------------------------------------------------------------------- #
# Task control (write — gated by require_write_token)
# ---------------------------------------------------------------------- #


@pytest.fixture()
def write_token(monkeypatch):
    """Configure CONSOLE_WRITE_TOKEN so the gate lets a matching header
    through, same fixture role as tests/unit/test_security.py's env setup."""
    from mlops_framework.config.settings import get_settings

    monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "test-token")
    get_settings.cache_clear()
    yield "test-token"
    get_settings.cache_clear()


class TestTaskControl:
    def test_clear_requires_token_when_none_configured(
        self, client, fake_airflow, run_on_airflow, monkeypatch
    ):
        from mlops_framework.config.settings import get_settings

        monkeypatch.delenv("CONSOLE_WRITE_TOKEN", raising=False)
        get_settings.cache_clear()
        r = client.post(f"/api/training-runs/{run_on_airflow}/tasks/train/clear")
        assert r.status_code == 503
        get_settings.cache_clear()

    def test_clear_rejects_missing_header(
        self, anon_client, fake_airflow, run_on_airflow, write_token
    ):
        # anon_client, not client: the shared `client` fixture now sends
        # the token on every request (see conftest), so it cannot express
        # "no header" — which is exactly what this test is about.
        r = anon_client.post(f"/api/training-runs/{run_on_airflow}/tasks/train/clear")
        assert r.status_code == 401

    def test_clear_rejects_wrong_token(self, client, fake_airflow, run_on_airflow, write_token):
        """401, not 403. Since scopes arrived, 403 means "we know who you
        are and you may not do this"; a secret that matches nobody is an
        authentication failure. See tests/api/test_write_auth.py's
        TestScopes for what now produces a 403."""
        r = client.post(
            f"/api/training-runs/{run_on_airflow}/tasks/train/clear",
            headers={"X-Console-Token": "nope"},
        )
        assert r.status_code == 401

    def test_clear_succeeds_with_correct_token(
        self, client, fake_airflow, run_on_airflow, write_token
    ):
        r = client.post(
            f"/api/training-runs/{run_on_airflow}/tasks/train/clear",
            headers={"X-Console-Token": write_token},
        )
        assert r.status_code == 200
        assert r.json() == {"task_id": "train", "action": "clear", "cleared_task_instances": 1}

    def test_retry_succeeds_with_correct_token(
        self, client, fake_airflow, run_on_airflow, write_token
    ):
        r = client.post(
            f"/api/training-runs/{run_on_airflow}/tasks/train/retry",
            headers={"X-Console-Token": write_token},
        )
        assert r.status_code == 200
        assert r.json()["action"] == "retry"

    def test_clear_missing_task_is_404(
        self, client, fake_airflow, run_on_airflow, write_token
    ):
        r = client.post(
            f"/api/training-runs/{run_on_airflow}/tasks/missing/clear",
            headers={"X-Console-Token": write_token},
        )
        assert r.status_code == 404

    def test_clear_unconfigured_airflow_is_503(
        self, client, run_on_airflow, write_token, monkeypatch
    ):
        monkeypatch.setattr(
            airflow_views, "client_or_reason", lambda: (None, "not configured")
        )
        r = client.post(
            f"/api/training-runs/{run_on_airflow}/tasks/train/clear",
            headers={"X-Console-Token": write_token},
        )
        assert r.status_code == 503

    def test_clear_unknown_run_is_404(self, client, fake_airflow, write_token):
        r = client.post(
            "/api/training-runs/9999/tasks/train/clear",
            headers={"X-Console-Token": write_token},
        )
        assert r.status_code == 404
