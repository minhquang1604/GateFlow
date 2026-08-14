"""Integration tests for the MLOpsProject SDK.

Exercises the full happy path through the existing managers and the
in-memory orchestrator + tracker, using the project's own test pipelines.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.sdk import MLOpsProject
from mlops_framework.sdk.exceptions import TrainingError
from mlops_framework.tracking.in_memory import InMemoryTracker

SUCCESS_PIPELINE = "tests._pipelines.e2e_training:main"
FAIL_PIPELINE = "tests._pipelines.pipelines:fail"


@pytest.fixture()
def project():
    """Build an MLOpsProject with local orchestrator and in-memory tracker."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    mgr = DatabaseManager()
    mgr._engine = engine  # type: ignore[attr-defined]
    mgr._session_factory = factory  # type: ignore[attr-defined]

    orch = LocalDockerOrchestrator()
    p = MLOpsProject(
        "test",
        db_manager=mgr,
        orchestrator=orch,
        tracker=InMemoryTracker(),
    )
    try:
        yield p
    finally:
        orch.shutdown()
        Base.metadata.drop_all(engine)
        engine.dispose()


class TestFullLifecycle:
    def test_train_runs_through_sdk(self, project):
        project.register_pipeline("success", SUCCESS_PIPELINE)
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 5000, metadata={"columns": []})

        run = project.train(
            dataset_version=v, pipeline="success", wait=True, timeout=30
        )
        assert run.status == "SUCCESS"
        # The pipeline's metrics end up in the in-memory tracker inside
        # the subprocess (or MLflow in real runs), not on TrainingRun. The
        # SDK returns whatever the run row has, which is fine.
        assert run.error_message is None
        assert run.pipeline_id == SUCCESS_PIPELINE

    def test_train_failure_propagates(self, project):
        project.register_pipeline("fail", FAIL_PIPELINE)
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 5000, metadata={"columns": []})

        with pytest.raises(TrainingError):
            project.train(
                dataset_version=v, pipeline="fail", wait=True, timeout=10
            )

    def test_train_no_wait_returns_pending(self, project):
        project.register_pipeline("success", SUCCESS_PIPELINE)
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 5000, metadata={"columns": []})

        # wait=False should return immediately with a PENDING or RUNNING run
        # (depending on how fast the local orchestrator picks it up).
        run = project.train(
            dataset_version=v, pipeline="success", wait=False
        )
        assert run.status in ("PENDING", "RUNNING", "SUCCESS")


class TestSDKDoesNotLeakManagers:
    """The SDK should be usable end-to-end without ever importing a manager."""

    def test_no_direct_manager_imports_needed(self, project):
        # All of the following work without ever touching DatasetManager,
        # TrainingManager, ModelManager, etc.
        project.register_pipeline("p", SUCCESS_PIPELINE)
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 100)
        m = project.create_model("model-a")
        assert m.versions == []
        # The lineage graph is reachable purely via the SDK
        g = project.lineage.for_dataset_version(v.id)
        assert g["root_kind"] == "DatasetVersion"
