"""MLOpsProject — the developer-facing SDK entry point.

This module is a thin façade over the existing framework. Every method
delegates to a Week 1-3 manager or service; the SDK adds:

* A single import (``from mlops_framework import MLOpsProject``)
* Friendly pipeline names via :class:`~mlops_framework.pipeline.PipelineRegistry`
* Lightweight value objects (read-only) instead of ORM rows
* SDK-level exceptions that don't leak the manager internals
* Helpers for readiness, lineage, and model promotion

No new business logic lives here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model_version import (
    ModelState,
    ModelVersion,
)
from mlops_framework.database.models.training_run import (
    RunStatus,
    TrainingRun,
)
from mlops_framework.database.session import (
    DatabaseManager,
    get_db_manager,
    set_db_manager,
)
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.exceptions import (
    DuplicateDatasetNameError,
    DuplicateModelNameError,
)
from mlops_framework.lineage.manager import LineageManager
from mlops_framework.model.manager import ModelManager
from mlops_framework.orchestration.base import Orchestrator
from mlops_framework.pipeline.manager import (
    PipelineEntry,
    PipelineNotFoundError,
    PipelineRegistry,
)
from mlops_framework.readiness.engine import (
    ReadinessEngine,
    ReadinessResult,
    TrainingPolicy,
)
from mlops_framework.sdk.exceptions import (
    AlreadyExistsError,
    NotFoundError,
    PipelineNotRegisteredError,
    TrainingError,
)
from mlops_framework.tracking.base import ExperimentTracker
from mlops_framework.tracking.in_memory import InMemoryTracker
from mlops_framework.training.manager import TrainingManager
from mlops_framework.training.service import TrainingService

# ---------------------------------------------------------------------- #
# Value objects (read-only, JSON-serializable)
# ---------------------------------------------------------------------- #


@dataclass
class MLOpsDatasetVersion:
    id: int
    dataset_id: int
    version_number: int
    storage_uri: str
    checksum: str
    schema_hash: str
    row_count: int
    metadata: dict

    @classmethod
    def from_orm(cls, v: DatasetVersion) -> MLOpsDatasetVersion:
        meta = {}
        if v.metadata_json:
            try:
                meta = json.loads(v.metadata_json)
            except (TypeError, ValueError):
                meta = {}
        return cls(
            id=v.id,
            dataset_id=v.dataset_id,
            version_number=v.version_number,
            storage_uri=v.storage_uri,
            checksum=v.checksum,
            schema_hash=v.schema_hash,
            row_count=v.row_count,
            metadata=meta,
        )


@dataclass
class MLOpsDataset:
    id: int
    name: str
    description: str | None
    _project: MLOpsProject

    @property
    def versions(self) -> list[MLOpsDatasetVersion]:
        with self._project._session_scope() as s:
            self._project._ensure_managers(s)
            return [
                MLOpsDatasetVersion.from_orm(v)
                for v in self._project.datasets.list_versions(self.id)
            ]

    @property
    def latest_version(self) -> MLOpsDatasetVersion | None:
        with self._project._session_scope() as s:
            self._project._ensure_managers(s)
            v = self._project.datasets.get_latest_version(self.id)
            return MLOpsDatasetVersion.from_orm(v) if v else None

    def create_version(
        self,
        storage_uri: str,
        row_count: int,
        metadata: dict | None = None,
    ) -> MLOpsDatasetVersion:
        with self._project._session_scope() as s:
            self._project._ensure_managers(s)
            v = self._project.datasets.create_version(
                dataset_id=self.id,
                storage_uri=storage_uri,
                row_count=row_count,
                metadata=metadata,
            )
            return MLOpsDatasetVersion.from_orm(v)


@dataclass
class MLOpsRun:
    id: int
    status: str
    pipeline_id: str
    dataset_version_id: int | None
    parameters: dict
    metrics: dict
    error_message: str | None

    @classmethod
    def from_orm(cls, r: TrainingRun) -> MLOpsRun:
        meta = {}
        if r.metadata_json:
            try:
                meta = json.loads(r.metadata_json)
            except (TypeError, ValueError):
                meta = {}
        return cls(
            id=r.id,
            status=r.status,
            pipeline_id=r.pipeline_id or "",
            dataset_version_id=r.dataset_version_id,
            parameters=meta.get("parameters", {}),
            metrics=meta.get("metrics", {}),
            error_message=r.error_message,
        )


@dataclass
class MLOpsModelVersion:
    id: int
    model_id: int
    version_number: int
    state: str
    metrics: dict
    dataset_version_id: int | None
    training_run_id: int | None
    artifact_uri: str | None

    @classmethod
    def from_orm(cls, v: ModelVersion) -> MLOpsModelVersion:
        metrics = {}
        if v.metrics_json:
            try:
                metrics = json.loads(v.metrics_json)
            except (TypeError, ValueError):
                metrics = {}
        return cls(
            id=v.id,
            model_id=v.model_id,
            version_number=v.version_number,
            state=v.state,
            metrics=metrics,
            dataset_version_id=v.dataset_version_id,
            training_run_id=v.training_run_id,
            artifact_uri=v.artifact_uri,
        )


@dataclass
class MLOpsModel:
    id: int
    name: str
    description: str | None
    task: str | None
    _project: MLOpsProject

    @property
    def versions(self) -> list[MLOpsModelVersion]:
        with self._project._session_scope() as s:
            self._project._ensure_managers(s)
            return [
                MLOpsModelVersion.from_orm(v)
                for v in self._project.models.list_model_versions(self.id)
            ]

    @property
    def production_version(self) -> MLOpsModelVersion | None:
        for v in self.versions:
            if v.state == ModelState.PRODUCTION.value:
                return v
        return None


@dataclass
class MLOpsLineage:
    _project: MLOpsProject

    def for_dataset_version(self, version_id: int) -> dict:
        with self._project._session_scope() as s:
            return LineageManager(s).graph_for_dataset_version(version_id).to_dict()

    def for_model_version(self, version_id: int) -> dict:
        with self._project._session_scope() as s:
            return LineageManager(s).graph_for_model_version(version_id).to_dict()

    def for_training_run(self, run_id: int) -> dict:
        with self._project._session_scope() as s:
            return LineageManager(s).graph_for_training_run(run_id).to_dict()


# ---------------------------------------------------------------------- #
# Project
# ---------------------------------------------------------------------- #


class MLOpsProject:
    """Top-level SDK entry point.

    Wraps a :class:`DatabaseManager` and a :class:`PipelineRegistry` and
    exposes a small set of high-level methods. Constructed either with
    explicit dependencies (for tests / embedded use) or with environment
    defaults.

    Example::

        project = MLOpsProject("fraud-detection")
        project.register_pipeline(
            "xgboost-training", "my_pkg.pipelines:train_xgb"
        )
        ds = project.create_dataset("transactions")
        v = ds.create_version("s3://b/v1.parquet", 100_000)
        run = project.train(dataset_version=v, pipeline="xgboost-training")
    """

    def __init__(
        self,
        name: str,
        *,
        db_manager: DatabaseManager | None = None,
        orchestrator: Orchestrator | None = None,
        tracker: ExperimentTracker | None = None,
        pipeline_registry: PipelineRegistry | None = None,
    ) -> None:
        self.name = name

        # If the caller didn't supply a DatabaseManager, use the global
        # one (configured via env). Tests can pass an isolated manager.
        if db_manager is None:
            existing = get_db_manager()
            if existing is None or getattr(existing, "_engine", None) is None:
                db_manager = DatabaseManager()
                set_db_manager(db_manager)
            else:
                db_manager = existing
        self._db = db_manager

        self.orchestrator = orchestrator
        self.tracker = tracker or InMemoryTracker()
        self.pipelines = pipeline_registry or PipelineRegistry()

        # Managers are created per session to keep the API simple.
        self.datasets = None  # type: ignore[assignment]
        self.models = None  # type: ignore[assignment]
        self.training = None  # type: ignore[assignment]
        self._service: TrainingService | None = None

    @classmethod
    def with_defaults(
        cls,
        name: str,
        *,
        db_url: str | None = None,
    ) -> MLOpsProject:
        """Build an MLOpsProject with the framework's default adapters.

        Uses the local Docker orchestrator and the in-memory tracker. This
        is the recommended entry point for case studies and small apps —
        they don't need to import the orchestrator or tracker modules.
        """
        # Local imports so case studies don't need them on their import
        # graph; the SDK takes care of wiring.
        from mlops_framework.orchestration.local import LocalDockerOrchestrator
        from mlops_framework.tracking.in_memory import InMemoryTracker

        return cls(
            name,
            orchestrator=LocalDockerOrchestrator(),
            tracker=InMemoryTracker(),
            db_manager=DatabaseManager() if db_url else None,
        )

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    def _session_scope(self):
        """Context manager: yields a fresh session, commits on success."""
        return self._db.get_session()

    def _ensure_managers(self, session: Session) -> None:
        """Build manager objects bound to ``session``."""
        self.datasets = DatasetManager(session)
        self.models = ModelManager(session)
        self.training = TrainingManager(session, self.datasets)
        self._service = TrainingService(
            training_manager=self.training,
            orchestrator=self.orchestrator,
            tracker=self.tracker,
        )

    # ------------------------------------------------------------------ #
    # Pipelines
    # ------------------------------------------------------------------ #

    def register_pipeline(
        self,
        name: str,
        pipeline_id: str,
        *,
        description: str = "",
        parameters: dict | None = None,
    ) -> None:
        self.pipelines.register(
            PipelineEntry(
                name=name,
                pipeline_id=pipeline_id,
                description=description,
                parameters=parameters or {},
            )
        )

    # ------------------------------------------------------------------ #
    # Datasets
    # ------------------------------------------------------------------ #

    def create_dataset(
        self,
        name: str,
        description: str | None = None,
    ) -> MLOpsDataset:
        with self._session_scope() as s:
            self._ensure_managers(s)
            try:
                ds = self.datasets.create_dataset(name=name, description=description)
            except DuplicateDatasetNameError as exc:
                raise AlreadyExistsError(str(exc)) from exc
            return MLOpsDataset(
                id=ds.id, name=ds.name, description=ds.description, _project=self
            )

    def get_dataset(self, name: str) -> MLOpsDataset:
        with self._session_scope() as s:
            self._ensure_managers(s)
            ds = self.datasets.get_dataset_by_name(name)
            if ds is None:
                raise NotFoundError(f"Dataset {name!r} not found")
            return MLOpsDataset(
                id=ds.id, name=ds.name, description=ds.description, _project=self
            )

    def list_datasets(self) -> list[MLOpsDataset]:
        with self._session_scope() as s:
            self._ensure_managers(s)
            return [
                MLOpsDataset(
                    id=ds.id, name=ds.name, description=ds.description, _project=self
                )
                for ds in self.datasets.list_datasets()
            ]

    # ------------------------------------------------------------------ #
    # Models
    # ------------------------------------------------------------------ #

    def create_model(
        self,
        name: str,
        task: str | None = None,
        description: str | None = None,
    ) -> MLOpsModel:
        with self._session_scope() as s:
            self._ensure_managers(s)
            try:
                m = self.models.create_model(
                    name=name, task=task, description=description
                )
            except DuplicateModelNameError as exc:
                raise AlreadyExistsError(str(exc)) from exc
            return MLOpsModel(
                id=m.id, name=m.name, description=m.description, task=m.task,
                _project=self,
            )

    def get_model(self, name: str) -> MLOpsModel:
        with self._session_scope() as s:
            self._ensure_managers(s)
            m = self.models.get_model_by_name(name)
            if m is None:
                raise NotFoundError(f"Model {name!r} not found")
            return MLOpsModel(
                id=m.id, name=m.name, description=m.description, task=m.task,
                _project=self,
            )

    def list_models(self) -> list[MLOpsModel]:
        with self._session_scope() as s:
            self._ensure_managers(s)
            return [
                MLOpsModel(
                    id=m.id, name=m.name, description=m.description, task=m.task,
                    _project=self,
                )
                for m in self.models.list_models()
            ]

    # ------------------------------------------------------------------ #
    # Training
    # ------------------------------------------------------------------ #

    def train(
        self,
        dataset_version: MLOpsDatasetVersion,
        pipeline: str,
        *,
        parameters: dict | None = None,
        wait: bool = True,
        timeout: float = 60.0,
    ) -> MLOpsRun:
        """Create a training run for ``dataset_version``.

        Args:
            dataset_version: The :class:`MLOpsDatasetVersion` to train on.
            pipeline: Friendly pipeline name (must be registered).
            parameters: Optional parameters to pass to the pipeline.
            wait: If True, block until the run completes.
            timeout: Maximum time to wait when ``wait=True``.

        Returns:
            A :class:`MLOpsRun`. Status will be ``SUCCESS``, ``FAILED``,
            or ``CANCELLED`` if ``wait=True``; ``PENDING`` / ``RUNNING`` if
            ``wait=False``.

        Raises:
            PipelineNotRegisteredError: if the pipeline name is unknown.
            TrainingError: if the run ends in ``FAILED`` and ``wait=True``.
        """
        try:
            pipeline_id = self.pipelines.resolve(pipeline)
        except PipelineNotFoundError as exc:
            raise PipelineNotRegisteredError(str(exc)) from exc

        with self._session_scope() as s:
            self._ensure_managers(s)
            metadata = {"parameters": parameters or {}}
            run = self._service.create_run(
                dataset_version_id=dataset_version.id,
                pipeline_id=pipeline_id,
                trigger_type="MANUAL",
                metadata=metadata,
            )
            run_id = run.id
            self._service.start_run(run_id)

            if not wait:
                return MLOpsRun.from_orm(run)

            self._service.wait_for_completion(run_id, timeout=timeout)
            # Re-fetch the run so the caller sees the final state.
            final_row = self.training.get_run(run_id)
            if final_row.status == RunStatus.FAILED.value:
                raise TrainingError(
                    f"Training run {run_id} failed: {final_row.error_message or 'unknown'}"
                )
            return MLOpsRun.from_orm(final_row)

    # ------------------------------------------------------------------ #
    # Readiness / Lineage (helpers)
    # ------------------------------------------------------------------ #

    def readiness(
        self,
        dataset_version: MLOpsDatasetVersion,
        policy: TrainingPolicy | None = None,
    ) -> ReadinessResult:
        """Run the readiness engine for ``dataset_version``.

        Returns a :class:`ReadinessResult`; call ``.is_ready()`` to check
        readiness. Persists the evaluation to the database.
        """
        with self._session_scope() as s:
            self._ensure_managers(s)
            engine = ReadinessEngine(s)
            version = self.datasets.get_version(dataset_version.id)
            return engine.evaluate(version, policy=policy or TrainingPolicy())

    @property
    def lineage(self) -> MLOpsLineage:
        return MLOpsLineage(_project=self)

    def report(self, model_version_id: int, *, format: str = "markdown") -> str:
        """Build a self-contained reproducibility report for a ModelVersion.

        Composes the full lineage chain, the dataset version's content
        hash, metrics/params (DB + live MLflow when available), and the
        readiness/drift/promotion decision trail plus the audit/alert
        rows touching this model version — everything needed to
        reproduce or cite this result in a paper. See ``sdk/report.py``
        for what each section pulls from and why.

        Args:
            model_version_id: The ModelVersion to report on.
            format: ``"markdown"`` (default) or ``"html"``.

        Returns:
            The report as a plain string — write it to a file yourself
            (``pathlib.Path(...).write_text(...)``); this method has no
            opinion on where a report belongs.
        """
        from mlops_framework.exceptions import ModelVersionNotFoundError
        from mlops_framework.sdk.report import build_report

        with self._session_scope() as s:
            try:
                return build_report(s, model_version_id, format=format)
            except ModelVersionNotFoundError as exc:
                raise NotFoundError(str(exc)) from exc
