"""Unit tests for the LineageManager."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import DriftEvaluation
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_promotion_event import (
    ModelPromotionEvent,
)
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessEvaluation,
)
from mlops_framework.database.models.serving_instance import ServingInstance
from mlops_framework.database.models.training_run import (
    RunStatus,
    TrainingRun,
)
from mlops_framework.lineage.manager import LineageManager


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _make_dataset(session, row_count=1000):
    ds = Dataset(name="fraud")
    session.add(ds)
    session.flush()
    dv = DatasetVersion(
        dataset_id=ds.id,
        version_number=1,
        storage_uri="s3://b/v1.csv",
        checksum="0" * 64,
        schema_hash="0" * 64,
        row_count=row_count,
        is_immutable=True,
    )
    session.add(dv)
    session.flush()
    return ds, dv


class TestDatasetVersionLineage:
    def test_graph_for_dataset_version(self, session):
        ds, dv = _make_dataset(session)
        mgr = LineageManager(session)
        graph = mgr.graph_for_dataset_version(dv.id)
        assert graph.root_kind == "DatasetVersion"
        assert graph.root_id == f"DatasetVersion:{dv.id}"
        types = {n.type for n in graph.nodes}
        assert "Dataset" in types
        assert "DatasetVersion" in types
        edges = {(e.source, e.target, e.type) for e in graph.edges}
        assert (
            f"Dataset:{ds.id}", f"DatasetVersion:{dv.id}", "has_version"
        ) in edges

    def test_includes_training_runs(self, session):
        ds, dv = _make_dataset(session)
        run = TrainingRun(
            dataset_version_id=dv.id,
            status=RunStatus.SUCCESS,
        )
        session.add(run)
        session.flush()
        graph = LineageManager(session).graph_for_dataset_version(dv.id)
        types = {n.type for n in graph.nodes}
        assert "TrainingRun" in types
        # Edge: DatasetVersion -> TrainingRun
        edge_set = {(e.source, e.target) for e in graph.edges}
        assert (
            f"DatasetVersion:{dv.id}", f"TrainingRun:{run.id}"
        ) in edge_set

    def test_graph_is_json_serializable(self, session):
        _, dv = _make_dataset(session)
        graph = LineageManager(session).graph_for_dataset_version(dv.id)
        json.dumps(graph.to_dict())


class TestModelVersionLineage:
    def test_graph_for_model_version(self, session):
        ds, dv = _make_dataset(session)
        run = TrainingRun(
            dataset_version_id=dv.id, status=RunStatus.SUCCESS
        )
        session.add(run)
        session.flush()
        m = ModelRow(name="fraud-model")
        session.add(m)
        session.flush()
        mv = ModelVersion(
            model_id=m.id,
            dataset_version_id=dv.id,
            training_run_id=run.id,
            version_number=1,
            state=ModelState.PRODUCTION,
        )
        session.add(mv)
        session.flush()
        # Serving instance
        si = ServingInstance(
            serving_instance_id="s-1",
            model_id=m.id,
            model_version_id=mv.id,
            is_active=True,
            reload_source="event",
        )
        session.add(si)
        session.flush()

        graph = LineageManager(session).graph_for_model_version(mv.id)
        types = {n.type for n in graph.nodes}
        assert "Model" in types
        assert "ModelVersion" in types
        assert "TrainingRun" in types
        assert "DatasetVersion" in types
        assert "ServingInstance" in types
        # Edge from ModelVersion to ServingInstance
        edge_targets = {(e.source, e.target, e.type) for e in graph.edges}
        assert any(
            e[2] == "served_by" and e[0] == f"ModelVersion:{mv.id}"
            for e in edge_targets
        )


class TestEmptyGraph:
    def test_unknown_dataset_version(self, session):
        graph = LineageManager(session).graph_for_dataset_version(9999)
        assert graph.nodes == []
        assert graph.edges == []

    def test_unknown_training_run(self, session):
        graph = LineageManager(session).graph_for_training_run(9999)
        assert graph.nodes == []
