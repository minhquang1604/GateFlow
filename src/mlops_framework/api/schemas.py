"""Pydantic DTOs for the API layer.

These are *read-only* representations of the ORM models. The API layer never
writes via these schemas — mutations go through the existing managers
(``DatasetManager``, ``TrainingManager``, ``ModelManager``) and the schemas
just shape their output for JSON.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from mlops_framework.lineage.manager import LineageGraph


# ---------------------------------------------------------------------- #
# Shared
# ---------------------------------------------------------------------- #


class ApiModel(BaseModel):
    """Base for all API schemas.

    Reads attribute values directly from ORM rows (``model_config.from_attributes
    = True``) so handlers can pass ORM objects straight in.
    """

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------- #
# Datasets
# ---------------------------------------------------------------------- #


class DatasetVersionOut(ApiModel):
    id: int
    dataset_id: int
    version_number: int
    storage_uri: str
    checksum: str
    schema_hash: str
    row_count: int
    is_immutable: bool
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_with_metadata(cls, obj) -> "DatasetVersionOut":
        """Build a schema from a ``DatasetVersion``, parsing metadata_json."""
        metadata = None
        if getattr(obj, "metadata_json", None):
            try:
                metadata = json.loads(obj.metadata_json)
            except (TypeError, ValueError):
                metadata = None
        return cls(
            id=obj.id,
            dataset_id=obj.dataset_id,
            version_number=obj.version_number,
            storage_uri=obj.storage_uri,
            checksum=obj.checksum,
            schema_hash=obj.schema_hash,
            row_count=obj.row_count,
            is_immutable=obj.is_immutable,
            metadata=metadata,
            created_at=getattr(obj, "created_at", None),
        )


class DatasetOut(ApiModel):
    id: int
    name: str
    description: Optional[str] = None
    version_count: int = 0
    latest_version: Optional[DatasetVersionOut] = None


# ---------------------------------------------------------------------- #
# Training runs
# ---------------------------------------------------------------------- #


class TrainingRunOut(ApiModel):
    id: int
    dataset_version_id: Optional[int] = None
    model_id: Optional[int] = None
    pipeline_id: Optional[str] = None
    status: str
    trigger_type: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    error_message: Optional[str] = None
    mlflow_run_id: Optional[str] = None
    orchestrator: Optional[str] = None
    execution_id: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    metrics: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_with_json(cls, obj) -> "TrainingRunOut":
        """Build a schema, parsing JSON columns.

        ``TrainingRun`` only persists a single ``metadata_json`` blob —
        parameters and metrics are stored as ``{"parameters": {...},
        "metrics": {...}}`` inside it. We extract those sub-keys when
        present and fall back gracefully when they aren't.
        """

        def _maybe_load(raw: str | None) -> Dict[str, Any] | None:
            if not raw:
                return None
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return None

        metadata = _maybe_load(getattr(obj, "metadata_json", None)) or {}
        if not isinstance(metadata, dict):
            metadata = {}

        # A pipeline's own report lands under `orchestrator_result` (see
        # TrainingService.wait_for_completion). Prefer the caller-supplied
        # keys when present so a hand-written run still shows, but fall
        # back to what the pipeline actually computed — otherwise every
        # real training run renders with no metrics at all.
        result = metadata.get("orchestrator_result")
        if not isinstance(result, dict):
            result = {}

        started = getattr(obj, "started_at", None)
        completed = getattr(obj, "completed_at", None)
        duration = None
        if started and completed:
            duration = max((completed - started).total_seconds(), 0.0)

        status = obj.status
        trigger = getattr(obj, "trigger_type", None)
        return cls(
            id=obj.id,
            dataset_version_id=getattr(obj, "dataset_version_id", None),
            model_id=getattr(obj, "model_id", None),
            pipeline_id=getattr(obj, "pipeline_id", None),
            status=getattr(status, "value", status),
            trigger_type=getattr(trigger, "value", trigger),
            started_at=started,
            completed_at=completed,
            duration_seconds=duration,
            error_message=getattr(obj, "error_message", None),
            mlflow_run_id=getattr(obj, "mlflow_run_id", None),
            orchestrator=metadata.get("orchestrator"),
            execution_id=metadata.get("orchestrator_execution_id"),
            parameters=metadata.get("parameters") or result.get("params") or None,
            metrics=metadata.get("metrics") or result.get("metrics") or None,
            metadata=metadata,
            created_at=getattr(obj, "created_at", None),
        )


# ---------------------------------------------------------------------- #
# Models
# ---------------------------------------------------------------------- #


class ModelVersionOut(ApiModel):
    id: int
    model_id: int
    version_number: int
    state: str
    dataset_version_id: Optional[int] = None
    training_run_id: Optional[int] = None
    artifact_uri: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_with_metrics(cls, obj) -> "ModelVersionOut":
        metrics = None
        raw = getattr(obj, "metrics_json", None)
        if raw:
            try:
                metrics = json.loads(raw)
            except (TypeError, ValueError):
                metrics = None
        return cls(
            id=obj.id,
            model_id=obj.model_id,
            version_number=obj.version_number,
            state=obj.state,
            dataset_version_id=getattr(obj, "dataset_version_id", None),
            training_run_id=getattr(obj, "training_run_id", None),
            artifact_uri=getattr(obj, "artifact_uri", None),
            metrics=metrics,
            notes=getattr(obj, "notes", None),
            created_at=getattr(obj, "created_at", None),
        )


class ModelOut(ApiModel):
    id: int
    name: str
    description: Optional[str] = None
    task: Optional[str] = None
    version_count: int = 0
    production_version: Optional[ModelVersionOut] = None


# ---------------------------------------------------------------------- #
# Dashboard
# ---------------------------------------------------------------------- #


class DashboardCounts(BaseModel):
    datasets: int
    dataset_versions: int
    total_runs: int
    active_runs: int
    success_runs: int
    failed_runs: int
    models: int
    production_models: int
    success_rate: float = Field(
        description="Completed-runs success rate (0.0 - 1.0). "
        "0.0 when no runs have completed yet."
    )


# ---------------------------------------------------------------------- #
# Lineage
# ---------------------------------------------------------------------- #


class LineageGraphOut(BaseModel):
    """JSON-serializable lineage graph.

    Mirrors :class:`mlops_framework.lineage.manager.LineageGraph` but as a
    Pydantic model so FastAPI can serve it directly.
    """

    root_kind: str
    root_id: str
    nodes: List[Dict[str, Any]]
    edges: List[Dict[str, Any]]

    @classmethod
    def from_graph(cls, graph: LineageGraph) -> "LineageGraphOut":
        return cls(**graph.to_dict())


# ---------------------------------------------------------------------- #
# Readiness
# ---------------------------------------------------------------------- #


class ReadinessEvaluationOut(ApiModel):
    id: int
    dataset_version_id: int
    status: str
    policy: Optional[Dict[str, Any]] = None
    checks: Optional[Dict[str, Any]] = None
    reasons: Optional[Any] = None
    created_at: Optional[datetime] = None

    @classmethod
    def from_orm_with_json(cls, obj) -> "ReadinessEvaluationOut":
        def _maybe_load(raw):
            if not raw:
                return None
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return None

        return cls(
            id=obj.id,
            dataset_version_id=obj.dataset_version_id,
            status=obj.status,
            policy=_maybe_load(getattr(obj, "policy_json", None)),
            checks=_maybe_load(getattr(obj, "checks_json", None)),
            reasons=_maybe_load(getattr(obj, "reasons_json", None)),
            created_at=getattr(obj, "created_at", None),
        )
