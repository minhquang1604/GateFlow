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
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
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


def _make_dataset(session, row_count=1000, name="fraud"):
    ds = Dataset(name=name)
    session.add(ds)
    session.flush()
    dv = _add_version(session, ds, version_number=1, row_count=row_count)
    return ds, dv


def _add_version(session, ds, *, version_number, row_count, parent_version_id=None):
    dv = DatasetVersion(
        dataset_id=ds.id,
        version_number=version_number,
        storage_uri=f"s3://b/v{version_number}.csv",
        checksum=f"{version_number}" * 64,
        schema_hash="0" * 64,
        row_count=row_count,
        is_immutable=True,
        parent_version_id=parent_version_id,
    )
    session.add(dv)
    session.flush()
    return dv


def _add_model_version(session, dv, run=None, *, version_number=1, state=ModelState.PRODUCTION, model=None):
    if model is None:
        model = ModelRow(name="fraud-model")
    if model.id is None:
        session.add(model)
        session.flush()
    mv = ModelVersion(
        model_id=model.id,
        dataset_version_id=dv.id,
        training_run_id=run.id if run is not None else None,
        version_number=version_number,
        state=state,
    )
    session.add(mv)
    session.flush()
    return model, mv


class TestDatasetVersionLineage:
    def test_graph_for_dataset_version(self, session):
        ds, dv = _make_dataset(session)
        mgr = LineageManager(session)
        graph = mgr.graph_for_dataset_version(dv.id)
        assert graph.root_kind == "DatasetVersion"
        assert graph.root_id == f"DatasetVersion:{dv.id}"

    def test_the_dataset_identity_is_folded_into_the_version_node(self, session):
        """One node, not two — the name lives in the label, not a
        separate Dataset node joined by 'has_version'."""
        ds, dv = _make_dataset(session, name="credit-card-fraud")
        graph = LineageManager(session).graph_for_dataset_version(dv.id)

        types = {n.type for n in graph.nodes}
        assert types == {"DatasetVersion"}
        node = next(n for n in graph.nodes if n.id == f"DatasetVersion:{dv.id}")
        assert node.label == "credit-card-fraud v1"
        assert node.attributes["dataset_name"] == "credit-card-fraud"
        assert node.attributes["row_count"] == dv.row_count
        edge_types = {e.type for e in graph.edges}
        assert "has_version" not in edge_types

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


class TestWholeDatasetLineage:
    """Every version of a dataset, in parallel — the "current state"
    view a reader lands on regardless of which node they started from."""

    def test_a_sibling_version_appears_even_when_rooted_elsewhere(self, session):
        ds, v1 = _make_dataset(session, name="fraud")
        v2 = _add_version(session, ds, version_number=2, row_count=2000, parent_version_id=v1.id)

        # Starting from V1's own node, V2 (built later, extending it)
        # is still visible — this is the "song song" (parallel) view.
        graph = LineageManager(session).graph_for_dataset_version(v1.id)
        ids = {n.id for n in graph.nodes}
        assert f"DatasetVersion:{v1.id}" in ids
        assert f"DatasetVersion:{v2.id}" in ids

    def test_the_derived_from_edge_connects_the_two_versions(self, session):
        ds, v1 = _make_dataset(session)
        v2 = _add_version(session, ds, version_number=2, row_count=2000, parent_version_id=v1.id)

        graph = LineageManager(session).graph_for_dataset_version(v2.id)
        edges = {(e.source, e.target, e.type) for e in graph.edges}
        assert (
            f"DatasetVersion:{v1.id}", f"DatasetVersion:{v2.id}", "derived_from"
        ) in edges

    def test_graph_for_dataset_roots_at_the_latest_version(self, session):
        ds, v1 = _make_dataset(session)
        v2 = _add_version(session, ds, version_number=2, row_count=2000, parent_version_id=v1.id)

        graph = LineageManager(session).graph_for_dataset(ds.id)
        assert graph.root_id == f"DatasetVersion:{v2.id}"
        ids = {n.id for n in graph.nodes}
        assert f"DatasetVersion:{v1.id}" in ids
        assert f"DatasetVersion:{v2.id}" in ids

    def test_each_versions_own_downstream_is_included(self, session):
        """V1's archived model and V2's production model both show up —
        two full branches in one graph, not just the versions."""
        ds, v1 = _make_dataset(session)
        run1 = TrainingRun(dataset_version_id=v1.id, status=RunStatus.SUCCESS)
        session.add(run1)
        session.flush()
        model, mv1 = _add_model_version(
            session, v1, run1, version_number=1, state=ModelState.ARCHIVED
        )

        v2 = _add_version(session, ds, version_number=2, row_count=2000, parent_version_id=v1.id)
        run2 = TrainingRun(dataset_version_id=v2.id, status=RunStatus.SUCCESS)
        session.add(run2)
        session.flush()
        _, mv2 = _add_model_version(
            session, v2, run2, version_number=2, state=ModelState.PRODUCTION, model=model
        )

        graph = LineageManager(session).graph_for_dataset(ds.id)
        ids = {n.id for n in graph.nodes}
        assert f"ModelVersion:{mv1.id}" in ids
        assert f"ModelVersion:{mv2.id}" in ids
        edges = {(e.source, e.target, e.type) for e in graph.edges}
        assert (f"TrainingRun:{run1.id}", f"ModelVersion:{mv1.id}", "produced") in edges
        assert (f"TrainingRun:{run2.id}", f"ModelVersion:{mv2.id}", "produced") in edges

    def test_a_model_version_with_no_training_run_still_appears(self, session):
        """Found via the direct FK, not by walking a run that doesn't
        exist — the case the old run-only walk could never reach."""
        ds, v1 = _make_dataset(session)
        _, mv = _add_model_version(session, v1, run=None, version_number=1)

        graph = LineageManager(session).graph_for_dataset(ds.id)
        ids = {n.id for n in graph.nodes}
        assert f"ModelVersion:{mv.id}" in ids

    def test_unknown_dataset_id_returns_an_empty_graph(self, session):
        graph = LineageManager(session).graph_for_dataset(9999)
        assert graph.nodes == []
        assert graph.edges == []


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
        assert types == {"DatasetVersion", "TrainingRun", "ModelVersion", "ServingInstance"}
        # Edge from ModelVersion to ServingInstance
        edge_targets = {(e.source, e.target, e.type) for e in graph.edges}
        assert any(
            e[2] == "served_by" and e[0] == f"ModelVersion:{mv.id}"
            for e in edge_targets
        )

    def test_the_model_identity_is_folded_into_the_version_node(self, session):
        ds, dv = _make_dataset(session)
        run = TrainingRun(dataset_version_id=dv.id, status=RunStatus.SUCCESS)
        session.add(run)
        session.flush()
        _, mv = _add_model_version(session, dv, run, model=ModelRow(name="fraud-xgboost"))

        graph = LineageManager(session).graph_for_model_version(mv.id)
        node = next(n for n in graph.nodes if n.id == f"ModelVersion:{mv.id}")
        assert node.label == "fraud-xgboost v1"
        edge_types = {e.type for e in graph.edges}
        assert "has_version" not in edge_types

    def test_no_redundant_direct_dataset_version_to_model_version_edge(
        self, session
    ):
        """The only path in is through the TrainingRun that actually
        produced it — 'trained_on' used to draw a second, redundant arrow
        straight from the DatasetVersion."""
        ds, dv = _make_dataset(session)
        run = TrainingRun(dataset_version_id=dv.id, status=RunStatus.SUCCESS)
        session.add(run)
        session.flush()
        _, mv = _add_model_version(session, dv, run)

        graph = LineageManager(session).graph_for_model_version(mv.id)
        edges = {(e.source, e.target, e.type) for e in graph.edges}
        assert (f"DatasetVersion:{dv.id}", f"ModelVersion:{mv.id}", "trained_on") not in edges
        assert (
            f"DatasetVersion:{dv.id}", f"TrainingRun:{run.id}", "trained_with"
        ) in edges
        assert (
            f"TrainingRun:{run.id}", f"ModelVersion:{mv.id}", "produced"
        ) in edges


class TestEmptyGraph:
    def test_unknown_dataset_version(self, session):
        graph = LineageManager(session).graph_for_dataset_version(9999)
        assert graph.nodes == []
        assert graph.edges == []

    def test_unknown_training_run(self, session):
        graph = LineageManager(session).graph_for_training_run(9999)
        assert graph.nodes == []
