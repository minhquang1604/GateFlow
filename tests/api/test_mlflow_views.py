"""Tests for the MLflow-backed views.

The tracking server is faked rather than run: these tests are about the
framework's own logic — path validation, payload shaping, degradation —
not about MLflow's behaviour. The real thing is exercised by hand against
a live server; what is pinned here is what a fake can prove
deterministically.

Follows the ``_FakeAirflowClient`` pattern in
``tests/unit/test_airflow_orchestrator.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mlops_framework.api import mlflow_gateway
from mlops_framework.api.routers import mlflow_views
from mlops_framework.database.models.training_run import RunStatus, TrainingRun


MLFLOW_RUN_ID = "abc123def456"


class _FakeFileInfo:
    def __init__(self, path: str, is_dir: bool = False, file_size=None):
        self.path = path
        self.is_dir = is_dir
        self.file_size = file_size


class _FakeMlflowClient:
    """Enough of MlflowClient for these endpoints, and nothing more."""

    def __init__(self, tmp_path: Path):
        self._tmp = tmp_path
        self.search_runs_kwargs = None

    # -- experiments ---------------------------------------------------- #

    def search_experiments(self):
        return [
            SimpleNamespace(
                experiment_id="7",
                name="fraud-detection",
                lifecycle_stage="active",
                artifact_location="s3://bucket/7",
                creation_time=1_700_000_000_000,
                last_update_time=1_700_000_000_000,
                tags={"team": "risk"},
            )
        ]

    def search_runs(self, **kwargs):
        self.search_runs_kwargs = kwargs
        return [
            SimpleNamespace(
                info=SimpleNamespace(
                    run_id="r1",
                    run_name="sweep-1",
                    status="FINISHED",
                    start_time=1_700_000_000_000,
                    end_time=1_700_000_100_000,
                    experiment_id="7",
                ),
                data=SimpleNamespace(
                    metrics={"f1": 0.8},
                    params={"max_depth": "6"},
                    tags={"stage": "sweep", "mlflow.log-model.history": "noise"},
                ),
            )
        ]

    def get_run(self, run_id):
        """A plain run with no parent — tests that care override this."""
        return SimpleNamespace(
            info=SimpleNamespace(
                run_id=run_id,
                run_name="standalone",
                status="FINISHED",
                start_time=1_700_000_000_000,
                experiment_id="7",
            ),
            data=SimpleNamespace(metrics={}, params={}, tags={}),
        )

    # -- artifacts ------------------------------------------------------ #

    def list_artifacts(self, run_id, path=None):
        if path in (None, ""):
            return [
                _FakeFileInfo("plots", is_dir=True),
                _FakeFileInfo("requirements.txt", file_size=48),
            ]
        if path == "plots":
            return [_FakeFileInfo("plots/confusion_matrix.png", file_size=1024)]
        return []

    def download_artifacts(self, run_id, path, dst_path):
        target = Path(dst_path) / Path(path).name
        target.write_text("artifact-body", encoding="utf-8")
        return str(target)


@pytest.fixture()
def fake_mlflow(monkeypatch, tmp_path):
    """Point the gateway at a fake client instead of a real server.

    Patched in two places because the name is looked up in two: ``panel``
    resolves it inside the gateway's own namespace, while the artifact
    download endpoint holds a reference bound by ``from ... import`` at
    module load. Patching only the gateway would leave that endpoint
    talking to a real MLflow and answering 503.
    """
    fake = _FakeMlflowClient(tmp_path)
    for module in (mlflow_gateway, mlflow_views):
        monkeypatch.setattr(module, "client_or_reason", lambda: (fake, None))
    return fake


def _make_run(session_factory, mlflow_run_id):
    """Insert a TrainingRun plus the dataset version it requires.

    dataset_version_id is NOT NULL with a foreign key, so a run cannot be
    inserted on its own — same shape as ``_seed`` in test_runs_api.py.
    """
    from mlops_framework.database.models.dataset import Dataset
    from mlops_framework.database.models.dataset_version import DatasetVersion

    s = session_factory()
    try:
        ds = Dataset(name=f"d-{mlflow_run_id or 'none'}")
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
        run = TrainingRun(
            dataset_version_id=dv.id,
            pipeline_id="p",
            status=RunStatus.SUCCESS.value,
            mlflow_run_id=mlflow_run_id,
        )
        s.add(run)
        s.commit()
        return run.id
    finally:
        s.close()


@pytest.fixture()
def run_with_mlflow(session_factory):
    return _make_run(session_factory, MLFLOW_RUN_ID)


# ---------------------------------------------------------------------- #
# Degradation
# ---------------------------------------------------------------------- #


class TestDegradesWithoutMlflow:
    """Every panel must answer 200 with a reason, never raise."""

    def test_experiments_unconfigured(self, client, monkeypatch):
        monkeypatch.setattr(mlflow_gateway, "tracking_uri", lambda: None)
        r = client.get("/api/mlflow/experiments")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert "MLFLOW_TRACKING_URI" in body["reason"]

    def test_panel_swallows_client_errors(self, monkeypatch):
        """A tracking server that raises degrades the panel, not the page."""
        boom = SimpleNamespace()
        monkeypatch.setattr(
            mlflow_gateway, "client_or_reason", lambda: (boom, None)
        )

        def explode(_client):
            raise RuntimeError("connection refused")

        result = mlflow_gateway.panel(explode)
        assert result.available is False
        assert "connection refused" in result.reason

    def test_http_limits_are_bounded_but_overridable(self, monkeypatch):
        """MLflow's defaults hang for minutes; the gateway must cap them."""
        monkeypatch.delenv("MLFLOW_HTTP_REQUEST_TIMEOUT", raising=False)
        monkeypatch.delenv("MLFLOW_HTTP_REQUEST_MAX_RETRIES", raising=False)
        mlflow_gateway._apply_http_limits()
        import os

        assert int(os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"]) <= 10
        assert int(os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"]) <= 2

        # An operator's own value wins.
        monkeypatch.setenv("MLFLOW_HTTP_REQUEST_TIMEOUT", "45")
        mlflow_gateway._apply_http_limits()
        assert os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"] == "45"


# ---------------------------------------------------------------------- #
# Experiments
# ---------------------------------------------------------------------- #


class TestExperiments:
    def test_list(self, client, fake_mlflow):
        body = client.get("/api/mlflow/experiments").json()
        assert body["available"] is True
        exp = body["data"]["experiments"][0]
        assert exp["experiment_id"] == "7"
        assert exp["tags"] == {"team": "risk"}

    def test_leaderboard_orders_by_metric(self, client, fake_mlflow):
        body = client.get(
            "/api/mlflow/experiments/7/runs?order_by=f1&direction=asc"
        ).json()
        assert body["data"]["order_by"] == "metrics.f1 ASC"
        assert fake_mlflow.search_runs_kwargs["order_by"] == ["metrics.f1 ASC"]

    def test_leaderboard_defaults_to_start_time(self, client, fake_mlflow):
        body = client.get("/api/mlflow/experiments/7/runs").json()
        assert body["data"]["order_by"] == "attributes.start_time DESC"

    def test_leaderboard_drops_log_model_noise(self, client, fake_mlflow):
        """mlflow.log-model.history is a serialized blob, not a useful tag."""
        run = client.get("/api/mlflow/experiments/7/runs").json()["data"]["runs"][0]
        assert run["tags"] == {"stage": "sweep"}


# ---------------------------------------------------------------------- #
# Artifacts — path handling is the security-sensitive part
# ---------------------------------------------------------------------- #


class TestArtifactPathSafety:
    """The path comes from the browser and is handed to a store that may
    map it onto the filesystem. Escapes must be refused, not sanitised."""

    @pytest.mark.parametrize(
        "bad",
        [
            "../../../etc/passwd",
            "/etc/passwd",
            "plots/../../../etc/passwd",
            "a/../../b",
            "..",
            "plots\\..\\..\\etc",
        ],
    )
    def test_escapes_rejected(self, client, fake_mlflow, run_with_mlflow, bad):
        r = client.get(
            f"/api/training-runs/{run_with_mlflow}/artifacts/raw", params={"path": bad}
        )
        assert r.status_code == 400, f"{bad!r} was not rejected"

    def test_normal_path_allowed(self, client, fake_mlflow, run_with_mlflow):
        r = client.get(
            f"/api/training-runs/{run_with_mlflow}/artifacts/raw",
            params={"path": "requirements.txt"},
        )
        assert r.status_code == 200
        assert r.content == b"artifact-body"

    def test_inner_path_allowed(self, client, fake_mlflow, run_with_mlflow):
        r = client.get(
            f"/api/training-runs/{run_with_mlflow}/artifacts/raw",
            params={"path": "plots/confusion_matrix.png"},
        )
        assert r.status_code == 200


class TestArtifactListing:
    def test_lists_directories_first(self, client, fake_mlflow, run_with_mlflow):
        body = client.get(f"/api/training-runs/{run_with_mlflow}/artifacts").json()
        entries = body["data"]["entries"]
        assert [e["name"] for e in entries] == ["plots", "requirements.txt"]
        assert entries[0]["is_dir"] is True

    def test_run_without_mlflow_id_is_409(self, client, fake_mlflow, session_factory):
        run_id = _make_run(session_factory, None)
        r = client.get(f"/api/training-runs/{run_id}/artifacts")
        assert r.status_code == 409

    def test_unknown_run_is_404(self, client, fake_mlflow):
        assert client.get("/api/training-runs/9999/artifacts").status_code == 404


# ---------------------------------------------------------------------- #
# Model signature
# ---------------------------------------------------------------------- #


class TestModelInfo:
    def test_signature_json_strings_are_decoded(
        self, client, fake_mlflow, run_with_mlflow, monkeypatch
    ):
        """MLmodel nests signature schemas as JSON *strings* inside YAML."""
        inputs = json.dumps([{"name": "amount", "type": "double"}])

        def list_artifacts(run_id, path=None):
            if path in (None, ""):
                return [_FakeFileInfo("model", is_dir=True)]
            if path == "model":
                return [_FakeFileInfo("model/MLmodel")]
            return []

        def download_artifacts(run_id, path, dst_path):
            target = Path(dst_path) / "MLmodel"
            target.write_text(
                "flavors:\n"
                "  sklearn:\n"
                "    sklearn_version: 1.9.0\n"
                "  python_function:\n"
                "    python_version: '3.11.9'\n"
                f"signature:\n  inputs: '{inputs}'\n"
                "mlflow_version: 2.20.3\n",
                encoding="utf-8",
            )
            return str(target)

        monkeypatch.setattr(fake_mlflow, "list_artifacts", list_artifacts)
        monkeypatch.setattr(fake_mlflow, "download_artifacts", download_artifacts)

        data = client.get(f"/api/training-runs/{run_with_mlflow}/model-info").json()["data"]
        assert data["found"] is True
        assert data["layout"] == "run-artifact"
        assert data["signature"]["inputs"] == [{"name": "amount", "type": "double"}]
        assert data["flavors"] == ["python_function", "sklearn"]
        assert data["flavor_detail"]["python_function"]["python_version"] == "3.11.9"

    def test_missing_model_explains_itself(
        self, client, fake_mlflow, run_with_mlflow
    ):
        """The framework logs models with log_artifact(), which writes no
        MLmodel — the panel has to say so rather than look broken."""
        data = client.get(f"/api/training-runs/{run_with_mlflow}/model-info").json()["data"]
        assert data["found"] is False
        assert "log_model" in data["note"]


# ---------------------------------------------------------------------- #
# Model registry reconciliation
# ---------------------------------------------------------------------- #


class _FakeModelVersion:
    def __init__(self, name, version, run_id, stage="None", status="READY"):
        self.name = name
        self.version = version
        self.run_id = run_id
        self.current_stage = stage
        self.status = status


class _FakeRegisteredModel:
    def __init__(self, name, aliases):
        self.name = name
        self.aliases = aliases


def _with_registry(fake, versions, aliases):
    """Attach a fake registry to the shared fake client."""
    fake.search_model_versions = lambda f="": [
        v
        for v in versions
        if (f"name='{v.name}'" == f or f"run_id='{v.run_id}'" == f or not f)
    ]
    fake.get_registered_model = lambda name: _FakeRegisteredModel(name, aliases)
    return fake


def _seed_model(session_factory, states_and_runs):
    """Create a Model with versions in the given states/MLflow run ids."""
    from mlops_framework.database.models.dataset import Dataset
    from mlops_framework.database.models.dataset_version import DatasetVersion
    from mlops_framework.database.models.model import Model as ModelRow
    from mlops_framework.database.models.model_version import ModelVersion

    s = session_factory()
    try:
        ds = Dataset(name="d-model")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://x",
            checksum="a" * 64, schema_hash="b" * 64, row_count=10,
        )
        s.add(dv)
        s.flush()
        model = ModelRow(name="fraud-xgboost")
        s.add(model)
        s.flush()
        for i, (state, run_id) in enumerate(states_and_runs, start=1):
            s.add(
                ModelVersion(
                    model_id=model.id, dataset_version_id=dv.id,
                    version_number=i, state=state, mlflow_run_id=run_id,
                )
            )
        s.commit()
        return model.id
    finally:
        s.close()


class TestRegistryReconciliation:
    def test_agreement_reports_no_drift(self, client, fake_mlflow, session_factory):
        model_id = _seed_model(session_factory, [("PRODUCTION", "run-a")])
        _with_registry(
            fake_mlflow,
            [_FakeModelVersion("fraud-xgboost", "1", "run-a")],
            {"champion": "1"},
        )
        d = client.get(f"/api/models/{model_id}/registry-reconciliation").json()["data"]
        assert d["drift_count"] == 0
        assert d["versions"][0]["mlflow_aliases"] == ["champion"]

    def test_production_without_alias_is_drift(
        self, client, fake_mlflow, session_factory
    ):
        """The case the panel exists for: promoted here, unblessed there."""
        model_id = _seed_model(session_factory, [("PRODUCTION", "run-a")])
        _with_registry(
            fake_mlflow, [_FakeModelVersion("fraud-xgboost", "1", "run-a")], {}
        )
        d = client.get(f"/api/models/{model_id}/registry-reconciliation").json()["data"]
        assert d["drift_count"] == 1
        assert "no production stage or alias" in d["versions"][0]["drift_reason"]

    def test_production_missing_from_registry_is_drift(
        self, client, fake_mlflow, session_factory
    ):
        model_id = _seed_model(session_factory, [("PRODUCTION", "run-missing")])
        _with_registry(fake_mlflow, [], {})
        d = client.get(f"/api/models/{model_id}/registry-reconciliation").json()["data"]
        assert d["drift_count"] == 1
        assert d["versions"][0]["in_mlflow_registry"] is False

    def test_rejected_version_absent_from_registry_is_not_drift(
        self, client, fake_mlflow, session_factory
    ):
        """A CANDIDATE or REJECTED version was never meant to be registered."""
        model_id = _seed_model(session_factory, [("REJECTED", "run-b")])
        _with_registry(fake_mlflow, [], {})
        d = client.get(f"/api/models/{model_id}/registry-reconciliation").json()["data"]
        assert d["drift_count"] == 0

    def test_legacy_production_stage_counts_as_serving(
        self, client, fake_mlflow, session_factory
    ):
        """MLflow 2.x used stages; a stage of Production still means served."""
        model_id = _seed_model(session_factory, [("PRODUCTION", "run-a")])
        _with_registry(
            fake_mlflow,
            [_FakeModelVersion("fraud-xgboost", "1", "run-a", stage="Production")],
            {},
        )
        d = client.get(f"/api/models/{model_id}/registry-reconciliation").json()["data"]
        assert d["drift_count"] == 0

    def test_unknown_model_is_404(self, client, fake_mlflow):
        assert (
            client.get("/api/models/9999/registry-reconciliation").status_code == 404
        )


# ---------------------------------------------------------------------- #
# Nested runs
# ---------------------------------------------------------------------- #


class TestNestedRuns:
    def _fake_run(self, run_id, parent=None, name="r"):
        return SimpleNamespace(
            info=SimpleNamespace(
                run_id=run_id, run_name=name, status="FINISHED",
                start_time=1, experiment_id="7",
            ),
            data=SimpleNamespace(
                metrics={"accuracy": 0.9}, params={"max_depth": "5"},
                tags={"mlflow.parentRunId": parent} if parent else {},
            ),
        )

    def test_child_reports_parent_and_siblings(
        self, client, fake_mlflow, run_with_mlflow, monkeypatch
    ):
        parent_id = "parent-1"
        runs = {
            MLFLOW_RUN_ID: self._fake_run(MLFLOW_RUN_ID, parent_id, "child-a"),
            parent_id: self._fake_run(parent_id, None, "sweep-parent"),
        }
        monkeypatch.setattr(fake_mlflow, "get_run", lambda rid: runs[rid])
        monkeypatch.setattr(
            fake_mlflow,
            "search_runs",
            lambda **kw: [
                self._fake_run(MLFLOW_RUN_ID, parent_id, "child-a"),
                self._fake_run("sib", parent_id, "child-b"),
            ],
        )
        d = client.get(f"/api/training-runs/{run_with_mlflow}/nested").json()["data"]
        assert d["is_child"] is True
        assert d["parent"]["run_name"] == "sweep-parent"
        assert [c["run_name"] for c in d["children"]] == ["child-a", "child-b"]
        assert [c["is_self"] for c in d["children"]] == [True, False]

    def test_standalone_run_has_no_tree(
        self, client, fake_mlflow, run_with_mlflow, monkeypatch
    ):
        monkeypatch.setattr(
            fake_mlflow, "get_run", lambda rid: self._fake_run(MLFLOW_RUN_ID)
        )
        monkeypatch.setattr(fake_mlflow, "search_runs", lambda **kw: [])
        d = client.get(f"/api/training-runs/{run_with_mlflow}/nested").json()["data"]
        assert d["is_child"] is False
        assert d["is_parent"] is False
        assert d["parent"] is None
        assert d["children"] == []
