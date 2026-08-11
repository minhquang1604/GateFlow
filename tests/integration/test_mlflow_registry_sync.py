"""mlflow_registry_sync against a real MLflow instance — a local,
sqlite-backed tracking server, no Docker and no network involved.

Not a stub: ``tests/integration/test_mlflow_live.py`` explains why a
real client/server round-trip matters here — a stub that fakes
``MlflowClient`` would agree with whatever shape the code under test
hands it, and could not catch e.g. a ``source`` URI MLflow's registry
actually rejects, or ``archive_existing_versions`` not doing what the
docstring assumes. Everything asserted below is read back through a
fresh :class:`MlflowClient` pointed at the same store, not off the
module under test.

MLflow's legacy filesystem store (``file:///…``) is disabled in the
installed SDK version ("in maintenance mode"), so the fixture uses a
throwaway ``sqlite:///…`` store per test instead — a real database
backend, just a local file rather than the deployed Postgres one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

from mlops_framework.config.settings import get_settings
from mlops_framework.tracking import mlflow_registry as sync

MODEL_NAME = "fraud-xgboost-regsync-test"


@pytest.fixture()
def mlflow_client(tmp_path, monkeypatch):
    """Point the framework's settings at a fresh local MLflow store and
    hand back a raw MlflowClient on the same store for assertions."""
    uri = f"sqlite:///{tmp_path}/mlflow.db"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    get_settings.cache_clear()

    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=uri)
    yield client
    get_settings.cache_clear()


def _run_with_artifact(client, filename: str = "model.json") -> str:
    exp = client.get_experiment_by_name("regsync-test")
    exp_id = exp.experiment_id if exp else client.create_experiment("regsync-test")
    run = client.create_run(exp_id)
    local_path = Path(tempfile.mkdtemp()) / filename
    local_path.write_text("{}")
    client.log_artifact(run.info.run_id, str(local_path))
    return run.info.run_id


class TestSyncCandidate:
    def test_registers_new_version(self, mlflow_client):
        run_id = _run_with_artifact(mlflow_client)
        version = sync.sync_candidate(MODEL_NAME, run_id)
        assert version == "1"
        mv = mlflow_client.get_model_version(MODEL_NAME, version)
        assert mv.run_id == run_id
        assert mv.source.endswith(f"runs:/{run_id}/model.json") or "model.json" in mv.source

    def test_second_candidate_gets_the_next_version_number(self, mlflow_client):
        v1 = sync.sync_candidate(MODEL_NAME, _run_with_artifact(mlflow_client))
        v2 = sync.sync_candidate(MODEL_NAME, _run_with_artifact(mlflow_client))
        assert v1 == "1"
        assert v2 == "2"

    def test_get_or_create_does_not_fail_on_an_existing_registered_model(self, mlflow_client):
        # The second call must not blow up on "registered model already
        # exists" — see _ensure_registered_model's RESOURCE_ALREADY_EXISTS
        # tolerance.
        sync.sync_candidate(MODEL_NAME, _run_with_artifact(mlflow_client))
        version = sync.sync_candidate(MODEL_NAME, _run_with_artifact(mlflow_client))
        assert version == "2"

    def test_no_mlflow_run_id_is_a_noop(self, mlflow_client):
        assert sync.sync_candidate(MODEL_NAME, None) is None

    def test_unreachable_tracking_server_returns_none_not_raise(self, monkeypatch):
        # A port nothing listens on refuses the connection immediately
        # rather than hanging — deterministic without a real timeout.
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1")
        get_settings.cache_clear()
        try:
            assert sync.sync_candidate(MODEL_NAME, "some-run-id") is None
        finally:
            get_settings.cache_clear()


class TestSyncProduction:
    def test_sets_stage_and_alias(self, mlflow_client):
        version = sync.sync_candidate(MODEL_NAME, _run_with_artifact(mlflow_client))
        assert sync.sync_production(MODEL_NAME, version) is True

        mv = mlflow_client.get_model_version(MODEL_NAME, version)
        assert mv.current_stage == "Production"
        registered = mlflow_client.get_registered_model(MODEL_NAME)
        # aliases map to the raw int version number, not the string
        # sync_candidate() returns — same cast mlflow_views.py's own
        # _alias_map() applies when reading this back for the UI.
        assert str(registered.aliases.get(sync.PRODUCTION_ALIAS)) == version

    def test_archives_the_previous_production_version(self, mlflow_client):
        v1 = sync.sync_candidate(MODEL_NAME, _run_with_artifact(mlflow_client))
        sync.sync_production(MODEL_NAME, v1)
        v2 = sync.sync_candidate(MODEL_NAME, _run_with_artifact(mlflow_client))
        sync.sync_production(MODEL_NAME, v2)

        old = mlflow_client.get_model_version(MODEL_NAME, v1)
        new = mlflow_client.get_model_version(MODEL_NAME, v2)
        assert old.current_stage == "Archived"
        assert new.current_stage == "Production"
        # The alias moved to the new version, not duplicated onto both.
        registered = mlflow_client.get_registered_model(MODEL_NAME)
        assert str(registered.aliases.get(sync.PRODUCTION_ALIAS)) == v2

    def test_no_version_is_a_noop(self, mlflow_client):
        assert sync.sync_production(MODEL_NAME, None) is False

    def test_unreachable_tracking_server_returns_false_not_raise(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:1")
        get_settings.cache_clear()
        try:
            assert sync.sync_production(MODEL_NAME, "1") is False
        finally:
            get_settings.cache_clear()


class TestArtifactFilenameFromUri:
    def test_extracts_basename_from_a_local_tmp_path(self):
        assert sync.artifact_filename_from_uri("/tmp/xyz123/model.json") == "model.json"

    def test_defaults_when_missing(self):
        assert sync.artifact_filename_from_uri(None) == sync.DEFAULT_ARTIFACT_FILENAME
        assert sync.artifact_filename_from_uri("") == sync.DEFAULT_ARTIFACT_FILENAME
