"""Integration tests for the TrainingManager lifecycle API.

Exercises the public methods:
    create_run, start_run, complete_run, fail_run, cancel_run,
    attach_mlflow_run, update_metadata, get_run, list_runs
"""

import pytest

from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    DatasetVersionNotFoundError,
    InvalidStatusTransitionError,
    TrainingRunNotFoundError,
)
from mlops_framework.training.manager import TrainingManager


def _create_dataset_version(db_session):
    """Helper: create a dataset and one version, return the version."""
    dm = DatasetManager(db_session)
    dataset = dm.create_dataset(name="test-dataset", description="Test")
    version = dm.create_version(
        dataset_id=dataset.id,
        storage_uri="s3://bucket/data.csv",
        row_count=1000,
    )
    return version


def _create_manager(db_session) -> TrainingManager:
    return TrainingManager(db_session, DatasetManager(db_session))


class TestCreateRun:
    def test_create_run_includes_pipeline_id(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(
            dataset_version_id=version.id,
            pipeline_id="fraud-training-pipeline",
        )
        assert run.id is not None
        assert run.pipeline_id == "fraud-training-pipeline"
        assert run.status.value == "PENDING"
        assert run.started_at is None
        assert run.completed_at is None
        assert run.mlflow_run_id is None
        assert run.error_message is None

    def test_create_run_invalid_version(self, db_session):
        mgr = _create_manager(db_session)
        with pytest.raises(DatasetVersionNotFoundError):
            mgr.create_run(dataset_version_id=9999)


class TestStartRun:
    def test_start_run_transitions_to_running(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)

        updated = mgr.start_run(run.id)
        assert updated.status.value == "RUNNING"
        assert updated.started_at is not None

    def test_start_run_attaches_mlflow_run_id(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id, mlflow_run_id="mlflow-abc-123")
        # detach session to ensure re-read
        db_session.expire_all()
        fetched = mgr.get_run(run.id)
        assert fetched.mlflow_run_id == "mlflow-abc-123"

    def test_start_run_from_running_rejected(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id)

        with pytest.raises(InvalidStatusTransitionError):
            mgr.start_run(run.id)


class TestCompleteRun:
    def test_complete_run_transitions_to_success(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id)

        done = mgr.complete_run(run.id)
        assert done.status.value == "SUCCESS"
        assert done.completed_at is not None

    def test_complete_run_from_pending_rejected(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        with pytest.raises(InvalidStatusTransitionError):
            mgr.complete_run(run.id)


class TestFailRun:
    def test_fail_run_records_error_message(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id)

        failed = mgr.fail_run(run.id, error_message="NaN in column 'amount'")
        assert failed.status.value == "FAILED"
        assert failed.error_message == "NaN in column 'amount'"
        assert failed.completed_at is not None

    def test_fail_run_from_pending_rejected(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        with pytest.raises(InvalidStatusTransitionError):
            mgr.fail_run(run.id, error_message="boom")


class TestCancelRun:
    def test_cancel_from_pending(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)

        cancelled = mgr.cancel_run(run.id)
        assert cancelled.status.value == "CANCELLED"
        assert cancelled.completed_at is not None

    def test_cancel_from_running(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id)

        cancelled = mgr.cancel_run(run.id)
        assert cancelled.status.value == "CANCELLED"

    def test_cancel_terminal_rejected(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id)
        mgr.complete_run(run.id)

        with pytest.raises(InvalidStatusTransitionError):
            mgr.cancel_run(run.id)


class TestBackwardsCompatTransitions:
    def test_success_to_running_rejected(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id)
        mgr.complete_run(run.id)

        with pytest.raises(InvalidStatusTransitionError):
            mgr.start_run(run.id)

    def test_failed_to_running_rejected(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.start_run(run.id)
        mgr.fail_run(run.id)

        with pytest.raises(InvalidStatusTransitionError):
            mgr.start_run(run.id)

    def test_cancelled_to_running_rejected(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.cancel_run(run.id)

        with pytest.raises(InvalidStatusTransitionError):
            mgr.start_run(run.id)


class TestAttachAndMetadata:
    def test_attach_mlflow_run(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(dataset_version_id=version.id)
        mgr.attach_mlflow_run(run.id, "mlflow-xyz")
        assert mgr.get_run(run.id).mlflow_run_id == "mlflow-xyz"

    def test_update_metadata_merges(self, db_session):
        version = _create_dataset_version(db_session)
        mgr = _create_manager(db_session)
        run = mgr.create_run(
            dataset_version_id=version.id,
            metadata={"model_type": "xgboost", "n_estimators": 100},
        )
        mgr.update_metadata(run.id, {"lr": 0.01})
        meta = mgr.get_run_metadata(run.id)
        assert meta["model_type"] == "xgboost"
        assert meta["n_estimators"] == 100
        assert meta["lr"] == 0.01

    def test_get_run_not_found(self, db_session):
        mgr = _create_manager(db_session)
        with pytest.raises(TrainingRunNotFoundError):
            mgr.get_run(9999)

    def test_list_runs_filters_by_version(self, db_session):
        dm = DatasetManager(db_session)
        mgr = _create_manager(db_session)
        ds = dm.create_dataset(name="ds", description="d")
        v1 = dm.create_version(dataset_id=ds.id, storage_uri="v1", row_count=1)
        v2 = dm.create_version(dataset_id=ds.id, storage_uri="v2", row_count=2)
        mgr.create_run(dataset_version_id=v1.id)
        mgr.create_run(dataset_version_id=v1.id)
        mgr.create_run(dataset_version_id=v2.id)

        assert len(mgr.list_runs()) == 3
        assert len(mgr.list_runs(dataset_version_id=v1.id)) == 2
        assert len(mgr.list_runs(dataset_version_id=v2.id)) == 1
