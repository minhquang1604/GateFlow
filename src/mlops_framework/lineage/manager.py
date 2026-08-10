"""Lineage manager — traverse the full end-to-end lineage chain.

The chain is:

    Dataset
        -> DatasetVersion(s)
        -> TrainingRun(s)
        -> ModelVersion(s)
        -> Production ModelVersion (at most one)
        -> ServingInstance(s) (the model versions that have been
                                loaded by serving processes)

The manager walks existing foreign keys; it does not duplicate
lineage data. It produces a serializable :class:`LineageGraph`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.serving_instance import ServingInstance
from mlops_framework.database.models.training_run import TrainingRun


# ---------------------------------------------------------------------- #
# Data classes
# ---------------------------------------------------------------------- #


@dataclass
class LineageNode:
    """A node in the lineage graph."""

    id: str
    type: str
    label: str
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "attributes": dict(self.attributes),
        }


@dataclass
class LineageEdge:
    """A directed edge between two lineage nodes."""

    source: str
    target: str
    type: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "target": self.target, "type": self.type}


@dataclass
class LineageGraph:
    """A complete lineage graph, serializable to JSON."""

    nodes: list[LineageNode] = field(default_factory=list)
    edges: list[LineageEdge] = field(default_factory=list)
    root_kind: str = ""
    root_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_kind": self.root_kind,
            "root_id": self.root_id,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }


# ---------------------------------------------------------------------- #
# Manager
# ---------------------------------------------------------------------- #


class LineageManager:
    """Walk the lineage chain and produce a :class:`LineageGraph`."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def graph_for_dataset_version(
        self, dataset_version_id: int
    ) -> LineageGraph:
        """Build a lineage graph rooted at a DatasetVersion."""
        dv = self._session.get(DatasetVersion, dataset_version_id)
        if dv is None:
            return LineageGraph(
                root_kind="DatasetVersion",
                root_id=str(dataset_version_id),
            )
        graph = LineageGraph(
            root_kind="DatasetVersion",
            root_id=f"DatasetVersion:{dv.id}",
        )
        # Dataset
        ds = self._session.get(Dataset, dv.dataset_id)
        if ds is not None:
            self._add_node(
                graph,
                LineageNode(
                    id=f"Dataset:{ds.id}",
                    type="Dataset",
                    label=ds.name,
                    attributes={"description": ds.description},
                ),
            )
            self._add_edge(
                graph,
                LineageEdge(
                    source=f"Dataset:{ds.id}",
                    target=f"DatasetVersion:{dv.id}",
                    type="has_version",
                ),
            )
        # DatasetVersion
        self._add_node(
            graph,
            LineageNode(
                id=f"DatasetVersion:{dv.id}",
                type="DatasetVersion",
                label=f"v{dv.version_number}",
                attributes={
                    "row_count": dv.row_count,
                    "schema_hash": dv.schema_hash,
                },
            ),
        )
        # Training runs
        for run in self._training_runs_for(dv.id):
            self._expand_training_run(graph, run)
        return graph

    def graph_for_training_run(self, training_run_id: int) -> LineageGraph:
        run = self._session.get(TrainingRun, training_run_id)
        if run is None:
            return LineageGraph(
                root_kind="TrainingRun",
                root_id=str(training_run_id),
            )
        graph = LineageGraph(
            root_kind="TrainingRun",
            root_id=f"TrainingRun:{run.id}",
        )
        self._expand_training_run(graph, run)
        return graph

    def graph_for_model_version(self, model_version_id: int) -> LineageGraph:
        mv = self._session.get(ModelVersion, model_version_id)
        if mv is None:
            return LineageGraph(
                root_kind="ModelVersion",
                root_id=str(model_version_id),
            )
        graph = LineageGraph(
            root_kind="ModelVersion",
            root_id=f"ModelVersion:{mv.id}",
        )
        # Model
        model = self._session.get(Model, mv.model_id)
        if model is not None:
            self._add_node(
                graph,
                LineageNode(
                    id=f"Model:{model.id}",
                    type="Model",
                    label=model.name,
                    attributes={"task": model.task},
                ),
            )
            self._add_edge(
                graph,
                LineageEdge(
                    source=f"Model:{model.id}",
                    target=f"ModelVersion:{mv.id}",
                    type="has_version",
                ),
            )
        # DatasetVersion
        dv = self._session.get(DatasetVersion, mv.dataset_version_id)
        if dv is not None:
            self._add_node(
                graph,
                LineageNode(
                    id=f"DatasetVersion:{dv.id}",
                    type="DatasetVersion",
                    label=f"v{dv.version_number}",
                ),
            )
            self._add_edge(
                graph,
                LineageEdge(
                    source=f"DatasetVersion:{dv.id}",
                    target=f"ModelVersion:{mv.id}",
                    type="trained_on",
                ),
            )
        # Training run
        if mv.training_run_id is not None:
            run = self._session.get(TrainingRun, mv.training_run_id)
            if run is not None:
                self._add_node(
                    graph,
                    LineageNode(
                        id=f"TrainingRun:{run.id}",
                        type="TrainingRun",
                        label=f"run {run.id}",
                        attributes={
                            "status": run.status,
                            "pipeline_id": run.pipeline_id,
                            "mlflow_run_id": run.mlflow_run_id,
                        },
                    ),
                )
                self._add_edge(
                    graph,
                    LineageEdge(
                        source=f"TrainingRun:{run.id}",
                        target=f"ModelVersion:{mv.id}",
                        type="produced",
                    ),
                )
        # ModelVersion itself
        self._add_node(
            graph,
            LineageNode(
                id=f"ModelVersion:{mv.id}",
                type="ModelVersion",
                label=f"v{mv.version_number}",
                attributes={"state": mv.state},
            ),
        )
        # Serving instances
        for si in self._serving_instances_for(mv.id):
            self._expand_serving_instance(graph, si)
        return graph

    # ------------------------------------------------------------------ #
    # Internal — node helpers
    # ------------------------------------------------------------------ #

    def _expand_training_run(
        self, graph: LineageGraph, run: TrainingRun
    ) -> None:
        # Link DatasetVersion -> TrainingRun
        dv = self._session.get(DatasetVersion, run.dataset_version_id)
        if dv is not None:
            if not self._has_node(graph, f"DatasetVersion:{dv.id}"):
                self._add_node(
                    graph,
                    LineageNode(
                        id=f"DatasetVersion:{dv.id}",
                        type="DatasetVersion",
                        label=f"v{dv.version_number}",
                    ),
                )
            self._add_edge(
                graph,
                LineageEdge(
                    source=f"DatasetVersion:{dv.id}",
                    target=f"TrainingRun:{run.id}",
                    type="trained_with",
                ),
            )
        self._add_node(
            graph,
            LineageNode(
                id=f"TrainingRun:{run.id}",
                type="TrainingRun",
                label=f"run {run.id}",
                attributes={
                    "status": run.status,
                    "pipeline_id": run.pipeline_id,
                    "mlflow_run_id": run.mlflow_run_id,
                },
            ),
        )
        # ModelVersions produced by this run
        for mv in self._model_versions_for_run(run.id):
            self._expand_model_version(graph, mv)

    def _expand_model_version(
        self, graph: LineageGraph, mv: ModelVersion
    ) -> None:
        model = self._session.get(Model, mv.model_id)
        if model is not None and not self._has_node(graph, f"Model:{model.id}"):
            self._add_node(
                graph,
                LineageNode(
                    id=f"Model:{model.id}",
                    type="Model",
                    label=model.name,
                ),
            )
            self._add_edge(
                graph,
                LineageEdge(
                    source=f"Model:{model.id}",
                    target=f"ModelVersion:{mv.id}",
                    type="has_version",
                ),
            )
        if not self._has_node(graph, f"ModelVersion:{mv.id}"):
            self._add_node(
                graph,
                LineageNode(
                    id=f"ModelVersion:{mv.id}",
                    type="ModelVersion",
                    label=f"v{mv.version_number}",
                    attributes={"state": mv.state},
                ),
            )
        if self._has_node(graph, f"TrainingRun:{mv.training_run_id}"):
            self._add_edge(
                graph,
                LineageEdge(
                    source=f"TrainingRun:{mv.training_run_id}",
                    target=f"ModelVersion:{mv.id}",
                    type="produced",
                ),
            )
        # Serving instances
        for si in self._serving_instances_for(mv.id):
            self._expand_serving_instance(graph, si)

    def _expand_serving_instance(
        self, graph: LineageGraph, si: ServingInstance
    ) -> None:
        node_id = f"ServingInstance:{si.serving_instance_id}:{si.id}"
        if not self._has_node(graph, node_id):
            self._add_node(
                graph,
                LineageNode(
                    id=node_id,
                    type="ServingInstance",
                    label=si.serving_instance_id,
                    attributes={
                        "is_active": si.is_active,
                        "reload_source": si.reload_source,
                    },
                ),
            )
        self._add_edge(
            graph,
            LineageEdge(
                source=f"ModelVersion:{si.model_version_id}",
                target=node_id,
                type="served_by",
            ),
        )

    # ------------------------------------------------------------------ #
    # Internal — query helpers
    # ------------------------------------------------------------------ #

    def _training_runs_for(
        self, dataset_version_id: int
    ) -> Iterable[TrainingRun]:
        return list(
            self._session.execute(
                select(TrainingRun)
                .where(TrainingRun.dataset_version_id == dataset_version_id)
                .order_by(TrainingRun.id)
            ).scalars().all()
        )

    def _model_versions_for_run(
        self, training_run_id: int
    ) -> Iterable[ModelVersion]:
        return list(
            self._session.execute(
                select(ModelVersion)
                .where(ModelVersion.training_run_id == training_run_id)
                .order_by(ModelVersion.id)
            ).scalars().all()
        )

    def _serving_instances_for(
        self, model_version_id: int
    ) -> Iterable[ServingInstance]:
        return list(
            self._session.execute(
                select(ServingInstance)
                .where(ServingInstance.model_version_id == model_version_id)
                .order_by(ServingInstance.id)
            ).scalars().all()
        )

    @staticmethod
    def _add_node(graph: LineageGraph, node: LineageNode) -> None:
        for existing in graph.nodes:
            if existing.id == node.id:
                existing.attributes.update(node.attributes)
                return
        graph.nodes.append(node)

    @staticmethod
    def _add_edge(graph: LineageGraph, edge: LineageEdge) -> None:
        for existing in graph.edges:
            if (
                existing.source == edge.source
                and existing.target == edge.target
                and existing.type == edge.type
            ):
                return
        graph.edges.append(edge)

    @staticmethod
    def _has_node(graph: LineageGraph, node_id: str) -> bool:
        return any(n.id == node_id for n in graph.nodes)
