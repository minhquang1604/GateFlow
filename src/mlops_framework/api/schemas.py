"""Pydantic DTOs for the API layer.

These are *read-only* representations of the ORM models. The API layer never
writes via these schemas — mutations go through the existing managers
(``DatasetManager``, ``TrainingManager``, ``ModelManager``) and the schemas
just shape their output for JSON.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

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


class ExternalPanel(BaseModel):
    """A panel the UI renders only when the backing system answered.

    Every view that reaches outside the framework's own database — MLflow,
    Airflow — returns this shape. ``available`` false plus ``reason`` lets
    the page say *why* a panel is empty ("no MLflow configured") instead of
    rendering a blank box that looks like a bug, and it keeps a tracking
    server being down from failing the whole page.
    """

    available: bool
    reason: str | None = None
    data: Any = None


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
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None

    @classmethod
    def from_orm_with_metadata(cls, obj) -> DatasetVersionOut:
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
    description: str | None = None
    version_count: int = 0
    latest_version: DatasetVersionOut | None = None


# ---------------------------------------------------------------------- #
# Training runs
# ---------------------------------------------------------------------- #


class TrainingRunOut(ApiModel):
    id: int
    dataset_version_id: int | None = None
    model_id: int | None = None
    pipeline_id: str | None = None
    status: str
    trigger_type: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    error_message: str | None = None
    mlflow_run_id: str | None = None
    orchestrator: str | None = None
    execution_id: str | None = None
    parameters: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None

    @classmethod
    def from_orm_with_json(cls, obj) -> TrainingRunOut:
        """Build a schema, parsing JSON columns.

        ``TrainingRun`` only persists a single ``metadata_json`` blob —
        parameters and metrics are stored as ``{"parameters": {...},
        "metrics": {...}}`` inside it. We extract those sub-keys when
        present and fall back gracefully when they aren't.
        """

        def _maybe_load(raw: str | None) -> dict[str, Any] | None:
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
    dataset_version_id: int | None = None
    training_run_id: int | None = None
    artifact_uri: str | None = None
    metrics: dict[str, Any] | None = None
    notes: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_orm_with_metrics(cls, obj) -> ModelVersionOut:
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
    description: str | None = None
    task: str | None = None
    version_count: int = 0
    production_version: ModelVersionOut | None = None


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
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]

    @classmethod
    def from_graph(cls, graph: LineageGraph) -> LineageGraphOut:
        return cls(**graph.to_dict())


# ---------------------------------------------------------------------- #
# Readiness
# ---------------------------------------------------------------------- #


class ReadinessEvaluationOut(ApiModel):
    id: int
    dataset_version_id: int
    status: str
    policy: dict[str, Any] | None = None
    checks: dict[str, Any] | None = None
    reasons: Any | None = None
    created_at: datetime | None = None

    @classmethod
    def from_orm_with_json(cls, obj) -> ReadinessEvaluationOut:
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


class DriftEvaluationOut(ApiModel):
    id: int
    reference_dataset_version_id: int
    current_dataset_version_id: int
    method: str
    outcome: str
    score: float | None = None
    threshold: float | None = None
    details: dict[str, Any] | None = None
    notes: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_orm_with_json(cls, obj) -> DriftEvaluationOut:
        details = None
        raw = getattr(obj, "details_json", None)
        if raw:
            try:
                details = json.loads(raw)
            except (TypeError, ValueError):
                details = None
        return cls(
            id=obj.id,
            reference_dataset_version_id=obj.reference_dataset_version_id,
            current_dataset_version_id=obj.current_dataset_version_id,
            method=obj.method,
            outcome=obj.outcome.value if hasattr(obj.outcome, "value") else obj.outcome,
            score=obj.score,
            threshold=obj.threshold,
            details=details,
            notes=obj.notes,
            created_at=getattr(obj, "created_at", None),
        )


# ---------------------------------------------------------------------- #
# Scheduling
# ---------------------------------------------------------------------- #


class ScheduleOut(ApiModel):
    id: int
    model_id: int
    model_name: str | None = None
    dataset_id: int
    dataset_name: str | None = None
    pipeline_id: str
    cron_expression: str
    enabled: bool
    parameters: dict[str, Any] | None = None
    min_f1: float
    last_triggered_at: datetime | None = None
    last_training_run_id: int | None = None
    # Computed, not stored — see scheduling/cron.py. None when the cron
    # expression somehow fails to evaluate (should not happen past
    # create/update validation, but a display field degrading to None
    # beats a 500 on the schedule list).
    next_fire_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_schedule(cls, obj) -> ScheduleOut:
        """Build from a ``Schedule`` ORM row.

        Named to avoid pydantic's own (deprecated) ``BaseModel.from_orm``
        — see this file's other ``from_orm_with_*`` builders for the
        same reason.
        """
        from mlops_framework.scheduling import cron

        parameters = None
        if getattr(obj, "parameters_json", None):
            try:
                parameters = json.loads(obj.parameters_json)
            except (TypeError, ValueError):
                parameters = None

        next_fire_at = None
        try:
            next_fire_at = cron.next_fire_time(
                obj.cron_expression, obj.last_triggered_at or obj.created_at
            )
        except Exception:  # noqa: BLE001 - a display field, never worth a 500
            pass

        return cls(
            id=obj.id,
            model_id=obj.model_id,
            model_name=getattr(getattr(obj, "model", None), "name", None),
            dataset_id=obj.dataset_id,
            dataset_name=getattr(getattr(obj, "dataset", None), "name", None),
            pipeline_id=obj.pipeline_id,
            cron_expression=obj.cron_expression,
            enabled=obj.enabled,
            parameters=parameters,
            min_f1=obj.min_f1,
            last_triggered_at=obj.last_triggered_at,
            last_training_run_id=obj.last_training_run_id,
            next_fire_at=next_fire_at,
            notes=obj.notes,
            created_at=getattr(obj, "created_at", None),
        )
