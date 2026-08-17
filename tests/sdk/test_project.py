"""Tests for the MLOpsProject SDK."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Registers every table on Base.metadata for create_all() in the
# fixture below. Without it this module passed only when another
# test had already imported framework_setting — see the models
# package docstring.
from mlops_framework.database import models  # noqa: F401
from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager
from mlops_framework.pipeline import PipelineNotFoundError
from mlops_framework.sdk import (
    AlreadyExistsError,
    MLOpsDataset,
    MLOpsProject,
    NotFoundError,
    PipelineNotRegisteredError,
)


@pytest.fixture()
def project():
    """Build an MLOpsProject backed by an isolated in-memory SQLite DB."""
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

    p = MLOpsProject("test", db_manager=mgr)
    yield p

    Base.metadata.drop_all(engine)
    engine.dispose()


class TestPipelines:
    def test_register_and_resolve(self, project):
        project.register_pipeline("xgb", "pkg:train")
        assert project.pipelines.resolve("xgb") == "pkg:train"

    def test_unregistered_raises(self, project):
        # The SDK's train() wraps the registry's PipelineNotFoundError into
        # the SDK's PipelineNotRegisteredError, so the registry exception
        # itself bubbles up when calling pipelines.resolve directly.
        with pytest.raises(PipelineNotFoundError):
            project.pipelines.resolve("nope")


class TestDatasets:
    def test_create_dataset(self, project):
        ds = project.create_dataset("ds1", description="d")
        assert isinstance(ds, MLOpsDataset)
        assert ds.name == "ds1"
        assert ds.versions == []
        assert ds.latest_version is None

    def test_duplicate_dataset_raises(self, project):
        project.create_dataset("dup")
        with pytest.raises(AlreadyExistsError):
            project.create_dataset("dup")

    def test_get_dataset(self, project):
        project.create_dataset("a")
        ds = project.get_dataset("a")
        assert ds.name == "a"

    def test_get_dataset_missing_raises(self, project):
        with pytest.raises(NotFoundError):
            project.get_dataset("nope")

    def test_list_datasets(self, project):
        project.create_dataset("a")
        project.create_dataset("b")
        assert {d.name for d in project.list_datasets()} == {"a", "b"}


class TestVersions:
    def test_create_and_list_versions(self, project):
        ds = project.create_dataset("ds")
        v1 = ds.create_version(
            "s3://b/v1", 100,
            metadata={"columns": [{"name": "x", "dtype": "int64"}]},
        )
        v2 = ds.create_version("s3://b/v2", 200)
        assert [v.version_number for v in ds.versions] == [1, 2]
        assert ds.latest_version.version_number == 2
        assert v1.row_count == 100
        assert v2.row_count == 200
        assert v1.metadata == {"columns": [{"name": "x", "dtype": "int64"}]}


class TestModels:
    def test_create_model(self, project):
        m = project.create_model("fraud-xgb", task="classification")
        assert m.name == "fraud-xgb"
        assert m.task == "classification"
        assert m.versions == []
        assert m.production_version is None

    def test_duplicate_model_raises(self, project):
        project.create_model("dup")
        with pytest.raises(AlreadyExistsError):
            project.create_model("dup")

    def test_get_and_list(self, project):
        project.create_model("a")
        project.create_model("b")
        assert {m.name for m in project.list_models()} == {"a", "b"}
        assert project.get_model("a").name == "a"


class TestReadiness:
    def test_default_policy_passes(self, project):
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 100)
        result = project.readiness(v)
        # Default policy has no required_size and no required columns,
        # so 100 rows should pass.
        assert result.is_ready is True


class TestLineage:
    def test_lineage_for_dataset_version(self, project):
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 100)
        g = project.lineage.for_dataset_version(v.id)
        assert g["root_kind"] == "DatasetVersion"
        types = {n["type"] for n in g["nodes"]}
        # The dataset's name lives on the version node's own label now,
        # not on a separate Dataset identity node — see LineageManager's
        # module docstring.
        assert types == {"DatasetVersion"}
        node = next(n for n in g["nodes"] if n["id"] == f"DatasetVersion:{v.id}")
        assert node["label"] == "ds v1"


class TestTrainErrors:
    def test_unknown_pipeline_raises(self, project):
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 100)
        with pytest.raises(PipelineNotRegisteredError):
            project.train(dataset_version=v, pipeline="nope", wait=False)


class TestRollback:
    """``MLOpsModel.rollback_to`` — addressed by version_number, which is
    what a reader sees, not the database id the rest of the SDK hides."""

    def _model_with_history(self, project):
        ds = project.create_dataset("ds")
        v = ds.create_version("s3://b/v1", 100)
        model = project.create_model("clf")
        from mlops_framework.database.models.model_version import ModelState

        with project._session_scope() as s:
            project._ensure_managers(s)
            mm = project.models
            a = mm.create_model_version(
                model_id=model.id, dataset_version_id=v.id, state=ModelState.CANDIDATE
            )
            b = mm.create_model_version(
                model_id=model.id, dataset_version_id=v.id, state=ModelState.CANDIDATE
            )
            for mv in (a, b):
                mm.transition_state(mv.id, ModelState.APPROVED)
            mm.transition_state(a.id, ModelState.PRODUCTION)
            mm.transition_state(a.id, ModelState.ARCHIVED)
            mm.transition_state(b.id, ModelState.PRODUCTION)
        return model

    def test_restores_by_version_number(self, project):
        model = self._model_with_history(project)
        assert model.production_version.version_number == 2

        restored = model.rollback_to(1)

        assert restored.version_number == 1
        assert model.production_version.version_number == 1

    def test_unknown_version_number_raises_not_found(self, project):
        model = self._model_with_history(project)
        with pytest.raises(NotFoundError):
            model.rollback_to(99)

    def test_rolling_back_to_the_live_version_is_refused(self, project):
        from mlops_framework.exceptions import RollbackError

        model = self._model_with_history(project)
        with pytest.raises(RollbackError):
            model.rollback_to(2)
