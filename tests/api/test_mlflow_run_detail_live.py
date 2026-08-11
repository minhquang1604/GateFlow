"""Live proof that a run reachable only by its raw MLflow run id — no
framework TrainingRun row at all — can be fully inspected through the
API: ``GET /mlflow/runs/{mlflow_run_id}`` (api/routers/runs.py, sibling
of the framework-run-scoped ``/training-runs/{run_id}/mlflow``) plus
its artifacts/model-info/nested siblings added alongside it in
``api/routers/mlflow_views.py``.

Real local (sqlite-backed) MLflow instance, same technique as
``tests/integration/test_mlflow_registry_sync.py`` — this is exactly
the "Experiments run I can't click into" gap the endpoint exists to
close, so the test deliberately never creates a TrainingRun row.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

from mlops_framework.api.app import create_app
from mlops_framework.config.settings import get_settings


@pytest.fixture()
def mlflow_client(tmp_path, monkeypatch):
    uri = f"sqlite:///{tmp_path}/mlflow.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    get_settings.cache_clear()

    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=uri)
    yield client
    get_settings.cache_clear()


@pytest.fixture()
def api(mlflow_client):
    app = create_app(mount_ui=False)
    return TestClient(app)


def _log_run(mlflow_client) -> str:
    exp_id = mlflow_client.create_experiment("run-detail-live-test")
    run = mlflow_client.create_run(exp_id, tags={"custom_tag": "hello"})
    run_id = run.info.run_id
    mlflow_client.log_param(run_id, "max_depth", "6")
    mlflow_client.log_metric(run_id, "f1", 0.80, step=0)
    mlflow_client.log_metric(run_id, "f1", 0.91, step=1)
    local = Path(tempfile.mkdtemp()) / "model.json"
    local.write_text("{}")
    mlflow_client.log_artifact(run_id, str(local))
    mlflow_client.set_terminated(run_id, status="FINISHED")
    return run_id


class TestRunSummaryByMlflowId:
    def test_returns_info_params_metrics_and_history(self, api, mlflow_client):
        run_id = _log_run(mlflow_client)

        resp = api.get(f"/api/mlflow/runs/{run_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is True

        data = body["data"]
        assert data["mlflow_run_id"] == run_id
        assert data["info"]["status"] == "FINISHED"
        assert data["params"]["max_depth"] == "6"
        assert data["tags"]["custom_tag"] == "hello"
        # metrics carries the *latest* value only, per MLflow's own Run.data
        assert data["metrics"]["f1"] == pytest.approx(0.91)
        # history carries every logged point, in step order.
        history = data["history"]["f1"]
        assert [p["value"] for p in history] == pytest.approx([0.80, 0.91])
        assert [p["step"] for p in history] == [0, 1]

    def test_unknown_run_id_degrades_the_panel_not_the_page(self, api, mlflow_client):
        resp = api.get("/api/mlflow/runs/does-not-exist")
        assert resp.status_code == 200  # ExternalPanel, never a 500/404
        body = resp.json()
        assert body["available"] is False


class TestArtifactsModelInfoNestedByMlflowId:
    """The three routes that used to only exist scoped to a framework
    run id now also work from a bare MLflow run id — same panels, same
    shape, just resolved without a TrainingRun lookup."""

    def test_artifacts_listing(self, api, mlflow_client):
        run_id = _log_run(mlflow_client)
        resp = api.get(f"/api/mlflow/runs/{run_id}/artifacts")
        assert resp.status_code == 200
        entries = resp.json()["data"]["entries"]
        assert any(e["name"] == "model.json" for e in entries)

    def test_artifact_download(self, api, mlflow_client):
        run_id = _log_run(mlflow_client)
        resp = api.get(f"/api/mlflow/runs/{run_id}/artifacts/raw?path=model.json")
        assert resp.status_code == 200
        assert resp.content == b"{}"

    def test_model_info_reports_no_mlmodel_for_a_raw_artifact(self, api, mlflow_client):
        # This framework's own pipeline logs model.json via log_artifact
        # (no signature) — see mlflow_views.py's note on why "found":
        # False is the common case, not a bug.
        run_id = _log_run(mlflow_client)
        resp = api.get(f"/api/mlflow/runs/{run_id}/model-info")
        assert resp.status_code == 200
        assert resp.json()["data"]["found"] is False

    def test_nested_runs_reports_a_standalone_run_as_neither(self, api, mlflow_client):
        run_id = _log_run(mlflow_client)
        resp = api.get(f"/api/mlflow/runs/{run_id}/nested")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["is_child"] is False
        assert data["is_parent"] is False
