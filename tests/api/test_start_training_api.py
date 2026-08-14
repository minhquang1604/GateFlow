"""``POST /api/training-runs`` — start a run from outside the DAG.

Training could previously only be started from Python (``project.train``)
or through ``/api/internal/*``, which is the Airflow DAG's own callback
surface — reachable, but not something a console button should call.
"""

from __future__ import annotations

import json

import pytest

from mlops_framework.database.models.audit_log import AuditLog
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.training_run import RunStatus, TrainingRun

ENTRYPOINT = "case_studies.fraud_detection.pipelines:train_xgboost"


@pytest.fixture()
def version_id(session_factory):
    s = session_factory()
    try:
        ds = Dataset(name="fraud")
        s.add(ds)
        s.flush()
        v = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            row_count=1000, checksum="c", schema_hash="h",
        )
        s.add(v)
        s.commit()
        return v.id
    finally:
        s.close()


@pytest.fixture()
def airflow_env(monkeypatch):
    from mlops_framework.config.settings import get_settings

    monkeypatch.setenv("AIRFLOW_BASE_URL", "http://airflow:8080")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def fake_airflow(monkeypatch):
    triggered = {}

    class _Fake:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def trigger_pipeline(self, pipeline_id, config=None):
            triggered["pipeline_id"] = pipeline_id
            triggered["config"] = config
            return f"{pipeline_id}/run-xyz"

        def get_execution_status(self, execution_id):  # pragma: no cover - unused
            raise NotImplementedError

    monkeypatch.setattr(
        "mlops_framework.orchestration.airflow.AirflowOrchestrator", _Fake
    )
    return triggered


class TestStart:
    def test_creates_and_starts_in_one_call(
        self, client, session_factory, version_id, airflow_env, fake_airflow
    ):
        r = client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
        )
        # 202: queued on Airflow, not finished.
        assert r.status_code == 202
        body = r.json()
        assert body["execution_id"] == "mlops_training_pipeline/run-xyz"
        assert body["pipeline_id"] == "mlops_training_pipeline"

        s = session_factory()
        try:
            run = s.get(TrainingRun, body["training_run_id"])
            # Not left PENDING — a created-but-unstarted run reads as
            # stuck to everyone looking at /runs.
            assert run.status == RunStatus.RUNNING.value
            assert run.dataset_version_id == version_id
        finally:
            s.close()

    def test_the_entrypoint_travels_in_metadata(
        self, client, session_factory, version_id, airflow_env, fake_airflow
    ):
        """pipeline_id is a dag_id to AirflowOrchestrator, so the real
        module:callable has to reach the DAG some other way."""
        r = client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
        )
        s = session_factory()
        try:
            run = s.get(TrainingRun, r.json()["training_run_id"])
            meta = json.loads(run.metadata_json)
            assert meta["training_entrypoint"] == ENTRYPOINT
        finally:
            s.close()

    def test_model_name_is_passed_through_when_given(
        self, client, session_factory, version_id, airflow_env, fake_airflow
    ):
        r = client.post(
            "/api/training-runs",
            json={
                "dataset_version_id": version_id,
                "training_entrypoint": ENTRYPOINT,
                "model_name": "fraud-xgboost",
                "min_f1": 0.6,
            },
        )
        s = session_factory()
        try:
            meta = json.loads(s.get(TrainingRun, r.json()["training_run_id"]).metadata_json)
            assert meta["model_name"] == "fraud-xgboost"
            assert meta["min_f1"] == 0.6
        finally:
            s.close()

    def test_no_model_name_registers_nothing(
        self, client, session_factory, version_id, airflow_env, fake_airflow
    ):
        """The right default for an exploratory run started by hand: the
        DAG trains and reports, but registers no ModelVersion."""
        r = client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
        )
        s = session_factory()
        try:
            meta = json.loads(s.get(TrainingRun, r.json()["training_run_id"]).metadata_json)
            assert "model_name" not in meta
        finally:
            s.close()

    def test_records_who_started_it(
        self, client, session_factory, version_id, airflow_env, fake_airflow
    ):
        client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
            headers={"X-Actor": "carol"},
        )
        s = session_factory()
        try:
            row = s.query(AuditLog).filter_by(action="TRAINING_RUN_STARTED").one()
            assert row.actor == "carol"
            assert json.loads(row.metadata_json)["training_entrypoint"] == ENTRYPOINT
        finally:
            s.close()


class TestRefusals:
    def test_unknown_dataset_version_is_404(self, client, airflow_env, fake_airflow):
        r = client.post(
            "/api/training-runs",
            json={"dataset_version_id": 9999, "training_entrypoint": ENTRYPOINT},
        )
        assert r.status_code == 404

    def test_without_airflow_configured_it_says_so(self, client, version_id, monkeypatch):
        from mlops_framework.config.settings import get_settings

        monkeypatch.delenv("AIRFLOW_BASE_URL", raising=False)
        get_settings.cache_clear()
        r = client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
        )
        assert r.status_code == 503
        assert "AIRFLOW_BASE_URL" in r.json()["detail"]
        get_settings.cache_clear()

    def test_a_failed_trigger_leaves_no_pending_run(
        self, client, session_factory, version_id, airflow_env, monkeypatch
    ):
        """The row exists and Airflow never took it — failing it here is
        what stops a PENDING run nothing will ever close."""

        class _Broken:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def trigger_pipeline(self, pipeline_id, config=None):
                raise OSError("airflow refused the connection")

        monkeypatch.setattr(
            "mlops_framework.orchestration.airflow.AirflowOrchestrator", _Broken
        )
        r = client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
        )
        assert r.status_code == 502

        s = session_factory()
        try:
            runs = s.query(TrainingRun).all()
            assert len(runs) == 1
            assert runs[0].status == RunStatus.FAILED.value
            assert "could not start" in (runs[0].error_message or "")
        finally:
            s.close()

    def test_is_gated(self, anon_client, session_factory, version_id, airflow_env, fake_airflow):
        r = anon_client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
        )
        assert r.status_code == 401

        s = session_factory()
        try:
            assert s.query(TrainingRun).count() == 0
        finally:
            s.close()


class TestListingIsUnaffected:
    def test_get_training_runs_still_reads(self, client, version_id, airflow_env, fake_airflow):
        """GET and POST share the path; the read half stays ungated."""
        client.post(
            "/api/training-runs",
            json={"dataset_version_id": version_id, "training_entrypoint": ENTRYPOINT},
        )
        assert len(client.get("/api/training-runs").json()) == 1
