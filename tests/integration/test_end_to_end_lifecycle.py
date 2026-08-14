"""End-to-end lifecycle test.

Flow under test:

    Dataset Version v5
        ↓
    Create Training Run (PENDING)
        ↓
    TrainingService.start_run()
        ↓
    LocalDockerOrchestrator
        ↓
    Training Pipeline (subprocess)
        ↓
    ExperimentTracker (InMemoryTracker)
        ↓
    ModelVersion registered (TRAINING -> CANDIDATE)
        ↓
    TrainingRun becomes SUCCESS

This test exercises the framework's public surface end-to-end, but
stays hermetic — no Airflow, no MLflow, no real data.
"""



from mlops_framework.database.models.model_version import ModelState
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.base import ExecutionState
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.tracking.in_memory import InMemoryTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService

E2E_PIPELINE = "tests._pipelines.e2e_training:main"


def _build_dataset(db_session) -> int:
    """Create a Dataset + 5 versions; return the v5 dataset_version_id."""
    dm = DatasetManager(db_session)
    dataset = dm.create_dataset(name="fraud-detection", description="Fraud data")
    last_id = None
    for i in range(1, 6):
        version = dm.create_version(
            dataset_id=dataset.id,
            storage_uri=f"s3://bucket/fraud-v{i}.csv",
            row_count=1000 * i,
            metadata={"columns": [{"name": "amount", "dtype": "float64"}]},
        )
        last_id = version.id
    return last_id


class TestEndToEndLifecycle:
    def test_full_lifecycle_success(self, db_session):
        dataset_version_id = _build_dataset(db_session)

        # 1. DatasetVersion v5 exists.
        dm = DatasetManager(db_session)
        dv = dm.get_version(dataset_version_id)
        assert dv.version_number == 5

        # 2. TrainingRun is created in PENDING.
        training_manager = TrainingManager(db_session, dm)
        model_manager = ModelManager(db_session)
        orchestrator = LocalDockerOrchestrator()
        tracker = InMemoryTracker()
        service = TrainingService(
            training_manager=training_manager,
            orchestrator=orchestrator,
            tracker=tracker,
        )
        model = model_manager.create_model(name="fraud-model", task="fraud_detection")
        run = service.create_run(
            dataset_version_id=dataset_version_id,
            pipeline_id=E2E_PIPELINE,
            metadata={"seed": 42, "pipeline_alias": "fraud-training-pipeline"},
        )
        assert run.status.value == "PENDING"
        assert run.id is not None

        # 3. Orchestrator is triggered and the run becomes RUNNING.
        execution_id = service.start_run(run.id)
        assert execution_id
        run = training_manager.get_run(run.id)
        assert run.status.value == "RUNNING"
        assert run.started_at is not None
        # Framework tracker was started; a tracker run id is attached.
        assert run.mlflow_run_id is not None
        # The framework's tracker has one open run, started by the service.
        assert len(tracker.runs) == 1
        assert tracker.runs[0]["ended"] is False  # ended on completion

        # 4. Wait for the pipeline to complete.
        try:
            final_state = service.wait_for_completion(run.id, timeout=30.0)
        finally:
            orchestrator.shutdown()

        assert final_state == ExecutionState.SUCCESS.value

        # 5. TrainingRun is now SUCCESS with timestamps.
        run = training_manager.get_run(run.id)
        assert run.status.value == "SUCCESS"
        assert run.completed_at is not None
        assert run.error_message is None

        # 6. Framework tracker was ended on completion.
        assert tracker.runs[0]["ended"] is True
        assert tracker.runs[0]["end_status"] == "SUCCESS"

        # 7. The pipeline (running in the subprocess) logged params,
        #    metrics, and an artifact. Those are captured by the
        #    orchestrator in the execution status metadata.
        exec_status = orchestrator.get_execution_status(execution_id)
        pipeline_metadata = exec_status.metadata
        assert pipeline_metadata.get("status") == "SUCCESS"
        assert "tracker_run_id" in pipeline_metadata
        assert "metrics" in pipeline_metadata
        assert "f1" in pipeline_metadata["metrics"]
        # The pipeline's tracker run id may differ from the framework's
        # tracker run id (they are separate processes) — both are
        # recorded for full lineage.
        assert pipeline_metadata["tracker_run_id"] != run.mlflow_run_id

        # 8. A ModelVersion is created and linked to all lineage.
        mv = model_manager.create_model_version(
            model_id=model.id,
            dataset_version_id=dataset_version_id,
            training_run_id=run.id,
            mlflow_run_id=pipeline_metadata["tracker_run_id"],
            artifact_uri=pipeline_metadata["artifact_path"],
            metrics=pipeline_metadata["metrics"],
            state=ModelState.TRAINING,
        )
        assert mv.training_run_id == run.id
        assert mv.dataset_version_id == dataset_version_id
        assert mv.mlflow_run_id == pipeline_metadata["tracker_run_id"]

        # 9. Promote the ModelVersion through the lifecycle.
        model_manager.transition_state(mv.id, ModelState.CANDIDATE)
        assert model_manager.get_model_version(mv.id).state == ModelState.CANDIDATE

        # 10. Lineage chain DatasetVersion -> TrainingRun -> ModelVersion.
        assert mv.dataset_version.id == dataset_version_id
        assert mv.training_run is not None
        assert mv.training_run.dataset_version_id == dataset_version_id
        assert mv.training_run.id == run.id

    def test_lifecycle_failure_propagates(self, db_session):
        """A failing pipeline should mark the training run FAILED with the
        error message captured from the orchestrator."""
        dataset_version_id = _build_dataset(db_session)

        training_manager = TrainingManager(db_session, DatasetManager(db_session))
        orchestrator = LocalDockerOrchestrator()
        tracker = InMemoryTracker()
        service = TrainingService(
            training_manager=training_manager,
            orchestrator=orchestrator,
            tracker=tracker,
        )
        # Use a pipeline that fails.
        run = service.create_run(
            dataset_version_id=dataset_version_id,
            pipeline_id="tests._pipelines.pipelines:fail",
        )
        service.start_run(run.id)
        try:
            final_state = service.wait_for_completion(run.id, timeout=10.0)
        finally:
            orchestrator.shutdown()

        assert final_state == ExecutionState.FAILED.value
        run = training_manager.get_run(run.id)
        assert run.status.value == "FAILED"
        assert run.error_message and "intentional failure" in run.error_message
        # Tracker run is ended as FAILED.
        assert tracker.runs[0]["ended"] is True
        assert tracker.runs[0]["end_status"] == "FAILED"
