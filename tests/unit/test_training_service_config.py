"""TrainingService: the config it hands a pipeline, and the result it keeps.

Both behaviours were missing and both broke real training in the same
way — a pipeline that could not find its data, and metrics that vanished
when the subprocess exited. The local orchestrator path had no
equivalent of the Airflow DAG's ``/internal/training-runs/{id}/context``
call, so ``LocalDockerOrchestrator`` could never run a real trainer.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.orchestration.base import (
    ExecutionState,
    ExecutionStatus,
    Orchestrator,
)
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService

STORAGE_URI = "s3://bucket/creditcard.csv"


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    ds = Dataset(name="credit-card-fraud")
    s.add(ds)
    s.flush()
    s.add(DatasetVersion(
        dataset_id=ds.id, version_number=1, storage_uri=STORAGE_URI,
        checksum="a" * 64, schema_hash="b" * 64, row_count=284807,
    ))
    s.commit()
    yield s
    s.close()


class _RecordingOrchestrator(Orchestrator):
    """Captures the config it was triggered with, then reports a result."""

    def __init__(self, result_metadata=None):
        self.triggered: list[tuple[str, dict]] = []
        self._result = result_metadata or {}

    def trigger_pipeline(self, pipeline_id, config=None):
        self.triggered.append((pipeline_id, dict(config or {})))
        return "exec-1"

    def get_execution_status(self, execution_id):
        return ExecutionStatus(
            execution_id=execution_id,
            state=ExecutionState.SUCCESS,
            metadata=self._result,
        )

    def cancel_execution(self, execution_id):
        return ExecutionStatus(execution_id=execution_id, state=ExecutionState.CANCELLED)


class _Tracker:
    tracking_uri = "http://mlflow.internal:5000"

    def start_run(self, run_name=None, tags=None):
        return "mlflow-run-1"

    def end_run(self, status="SUCCESS"):
        pass


def _service(session, orchestrator, tracker=None):
    dm = DatasetManager(session)
    return TrainingService(
        training_manager=TrainingManager(session, dm),
        orchestrator=orchestrator,
        tracker=tracker,
    )


class TestPipelineConfig:
    def test_forwards_the_dataset_location(self, session):
        """Without this a real trainer has nothing to open."""
        orch = _RecordingOrchestrator()
        svc = _service(session, orch)
        run = svc.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        svc.start_run(run.id)

        _, config = orch.triggered[0]
        assert config["csv_uri"] == STORAGE_URI
        assert config["storage_uri"] == STORAGE_URI
        assert config["training_run_id"] == run.id
        assert config["dataset_version_id"] == 1

    def test_forwards_the_tracker_run_and_uri(self, session):
        """A subprocess cannot inherit this process's mlflow global state."""
        orch = _RecordingOrchestrator()
        svc = _service(session, orch, tracker=_Tracker())
        run = svc.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        svc.start_run(run.id)

        _, config = orch.triggered[0]
        assert config["tracker_run_id"] == "mlflow-run-1"
        assert config["tracking_uri"] == "http://mlflow.internal:5000"

    def test_merges_caller_parameters_last(self, session):
        orch = _RecordingOrchestrator()
        svc = _service(session, orch)
        run = svc.create_run(
            dataset_version_id=1,
            pipeline_id="pkg.mod:train",
            metadata={"parameters": {"n_estimators": 60, "max_depth": 3}},
        )
        svc.start_run(run.id)

        _, config = orch.triggered[0]
        assert config["n_estimators"] == 60
        assert config["max_depth"] == 3

    def test_omits_tracker_keys_when_no_tracker(self, session):
        orch = _RecordingOrchestrator()
        svc = _service(session, orch)
        run = svc.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        svc.start_run(run.id)

        _, config = orch.triggered[0]
        assert "tracker_run_id" not in config
        assert "tracking_uri" not in config

    def test_matches_the_keys_the_airflow_dag_builds(self, session):
        """The two orchestration paths must hand a pipeline the same thing.

        ``infrastructure/airflow/dags/mlops_training_pipeline.py`` builds
        its training config from the internal context endpoint; a pipeline
        registered once has to run unchanged on either path.
        """
        orch = _RecordingOrchestrator()
        svc = _service(session, orch, tracker=_Tracker())
        run = svc.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        svc.start_run(run.id)

        _, config = orch.triggered[0]
        dag_keys = {
            "training_run_id", "dataset_version_id",
            "csv_uri", "tracker_run_id", "tracking_uri",
        }
        assert dag_keys <= set(config)


class TestResultPersistence:
    def test_pipeline_result_is_stored_on_the_run(self, session):
        """Otherwise the metrics die with the subprocess.

        The orchestrator surfaces them on ExecutionStatus.metadata; if
        nothing reads it there is no way to register a ModelVersion from
        a finished run.
        """
        result = {
            "status": "SUCCESS",
            "metrics": {"f1": 0.86, "average_precision": 0.88},
            "params": {"n_estimators": 200},
            "artifact_path": "/tmp/model.json",
        }
        orch = _RecordingOrchestrator(result_metadata=result)
        svc = _service(session, orch)
        run = svc.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        svc.start_run(run.id)
        svc.wait_for_completion(run.id, timeout=5.0, poll_interval=0.01)

        meta = TrainingManager(session).get_run_metadata(run.id)
        assert meta["orchestrator_result"]["metrics"]["f1"] == 0.86
        assert meta["orchestrator_result"]["artifact_path"] == "/tmp/model.json"

    def test_surfaces_through_the_api_schema(self, session):
        """The UI reads metrics off TrainingRunOut, not off the raw blob."""
        from mlops_framework.api.schemas import TrainingRunOut

        orch = _RecordingOrchestrator(result_metadata={
            "metrics": {"f1": 0.86}, "params": {"n_estimators": 200},
        })
        svc = _service(session, orch)
        run = svc.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        svc.start_run(run.id)
        svc.wait_for_completion(run.id, timeout=5.0, poll_interval=0.01)

        out = TrainingRunOut.from_orm_with_json(TrainingManager(session).get_run(run.id))
        assert out.metrics == {"f1": 0.86}
        assert out.parameters == {"n_estimators": 200}
        assert out.duration_seconds is not None and out.duration_seconds >= 0

    def test_does_not_clobber_a_result_already_reported(self, session):
        """AirflowOrchestrator's real shape: ExecutionStatus.metadata is only
        DAG-run-level info (logical_date/conf/...), never the pipeline's own
        metrics/params — those already landed in orchestrator_result via
        POST /internal/training-runs/{id}/finish (mlops_training_pipeline
        .py's report_status task, which runs as part of the same DAG,
        before this poll loop ever sees a terminal state). A blind
        overwrite here would discard that the instant it noticed
        completion — this pins the merge instead.
        """
        tm = TrainingManager(session)
        # No "metrics" key — exactly what AirflowOrchestrator._to_status()
        # actually returns.
        orch = _RecordingOrchestrator(result_metadata={"logical_date": "2026-08-11T00:00:00Z"})
        svc = _service(session, orch)
        run = svc.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        svc.start_run(run.id)
        # Simulates report_status's POST /finish having already run.
        tm.update_metadata(
            run.id,
            {
                "orchestrator_result": {
                    "metrics": {"f1": 0.83},
                    "params": {"n_estimators": 200},
                    "artifact_path": "s3://bucket/model.json",
                }
            },
        )

        svc.wait_for_completion(run.id, timeout=5.0, poll_interval=0.01)

        meta = tm.get_run_metadata(run.id)
        result = meta["orchestrator_result"]
        assert result["metrics"] == {"f1": 0.83}
        assert result["artifact_path"] == "s3://bucket/model.json"
        # The orchestrator's own (metrics-less) status is merged in too,
        # not dropped.
        assert result["logical_date"] == "2026-08-11T00:00:00Z"
