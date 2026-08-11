"""Internal endpoints for the Airflow DAG.

The framework's Airflow image no longer installs ``mlops_framework``
directly (see ``infrastructure/airflow/Dockerfile`` — Airflow 2.10.4
pins SQLAlchemy 1.4.x internally, which conflicts with the framework's
own ``sqlalchemy>=2.0.0`` requirement). ``infrastructure/airflow/dags/
mlops_training_pipeline.py`` calls these endpoints over HTTP instead of
importing the framework's ORM models and managers directly.

These routes intentionally do the same work
``mlops_training_pipeline.py`` used to do in-process: resolve a
TrainingRun + its DatasetVersion, and apply the model promotion
policy after a training run completes.

They are also the *only* way anything outside the VPC can write to the
framework's database. RDS is reachable from the app's security group and
nowhere else, so an application that wants to register a dataset or start
a training run against the deployed stack has to come through here. The
handlers are thin: each one calls the same manager an in-process caller
would, so there is no second implementation of the lifecycle rules.

Creation endpoints are **get-or-create** where a natural key exists
(dataset name, model name). Registering the same dataset twice is a
normal thing for a client script to do on a re-run, and returning 409
would push the retry logic into every caller.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import (
    get_dataset_manager,
    get_db,
    get_model_manager,
    get_training_manager,
)
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.database.models.training_run import TrainingRun
from mlops_framework.dataset.manager import DatasetManager
from mlops_framework.governance.promotion import (
    ModelPromotionPolicy,
    PromotionConfig,
    PromotionContext,
)
from mlops_framework.model.manager import ModelManager
from mlops_framework.readiness.engine import ReadinessEngine, TrainingPolicy
from mlops_framework.tracking import mlflow_registry as regsync
from mlops_framework.training.manager import TrainingManager

_log = logging.getLogger("mlops_framework.api.internal")

router = APIRouter()


# ---------------------------------------------------------------------- #
# GET /internal/training-runs/{run_id}/context
# ---------------------------------------------------------------------- #


class TrainingRunContextOut(BaseModel):
    training_run_id: int
    dataset_version_id: int
    storage_uri: str
    row_count: int
    pipeline_id: str | None = None
    metadata: dict[str, Any] = {}
    # The registered file digest, so a remote worker can confirm it read
    # the same bytes. None when the version was registered without one.
    dataset_content_sha256: str | None = None


@router.get(
    "/internal/training-runs/{run_id}/context",
    response_model=TrainingRunContextOut,
)
def get_training_run_context(
    run_id: int,
    db: Session = Depends(get_db),
) -> TrainingRunContextOut:
    """Resolve a TrainingRun + its DatasetVersion for the Airflow DAG.

    Replaces the DAG's old in-process ``resolve_context`` task, which
    imported ``TrainingRun``/``DatasetVersion`` directly.
    """
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    dataset_version = db.get(DatasetVersion, run.dataset_version_id)
    if dataset_version is None:
        raise HTTPException(
            status_code=404,
            detail=f"DatasetVersion {run.dataset_version_id} not found",
        )
    try:
        version_meta = json.loads(dataset_version.metadata_json or "{}")
    except (TypeError, ValueError):
        version_meta = {}
    return TrainingRunContextOut(
        training_run_id=run.id,
        dataset_version_id=dataset_version.id,
        storage_uri=dataset_version.storage_uri,
        row_count=dataset_version.row_count,
        pipeline_id=run.pipeline_id,
        metadata=json.loads(run.metadata_json or "{}"),
        dataset_content_sha256=version_meta.get("content_sha256"),
    )


# ---------------------------------------------------------------------- #
# POST /internal/models/{model_name}/promote
# ---------------------------------------------------------------------- #


class PromoteModelRequest(BaseModel):
    dataset_version_id: int
    training_run_id: int
    mlflow_run_id: str | None = None
    metrics: dict[str, Any] = {}
    artifact_uri: str | None = None
    min_f1: float = 0.0


class PromoteModelResponse(BaseModel):
    promoted: bool
    model_version_id: int
    model_version: int | None = None
    reasons: list[str] = []


@router.post(
    "/internal/models/{model_name}/promote",
    response_model=PromoteModelResponse,
)
def promote_model(
    model_name: str,
    request: PromoteModelRequest,
    db: Session = Depends(get_db),
    mm: ModelManager = Depends(get_model_manager),
) -> PromoteModelResponse:
    """Create a CANDIDATE model version and apply the promotion policy.

    Replaces the DAG's old in-process ``register_and_promote`` task.
    The model row must already exist (created by the demo / app code);
    this endpoint fails loudly otherwise, same as the original task.
    """
    model_row = mm.get_model_by_name(model_name)
    if model_row is None:
        raise HTTPException(
            status_code=404, detail=f"Model {model_name!r} not registered"
        )

    candidate = mm.create_model_version(
        model_id=model_row.id,
        dataset_version_id=request.dataset_version_id,
        training_run_id=request.training_run_id,
        mlflow_run_id=request.mlflow_run_id,
        state=ModelState.CANDIDATE,
        metrics=request.metrics,
        artifact_uri=request.artifact_uri,
    )
    # Registered on MLflow's side the moment the candidate exists — not
    # only once/if it is promoted — so MLflow's registry reflects every
    # training attempt the framework recorded, same as its own table
    # does. Never blocks or fails this request; see mlflow_registry_sync's
    # module docstring.
    mlflow_version = regsync.sync_candidate(
        model_row.name,
        request.mlflow_run_id,
        regsync.artifact_filename_from_uri(request.artifact_uri),
    )

    production = db.execute(
        select(ModelVersion)
        .where(
            ModelVersion.model_id == model_row.id,
            ModelVersion.state == ModelState.PRODUCTION,
        )
        .limit(1)
    ).scalars().first()

    decision = ModelPromotionPolicy().evaluate(
        context=PromotionContext(candidate=candidate, production=production),
        config=PromotionConfig(
            min_metrics={"f1": request.min_f1},
            must_beat_production=False,
            allow_cold_start=True,
        ),
    )
    if not decision.approved:
        mm.transition_state(candidate.id, ModelState.REJECTED)
        return PromoteModelResponse(
            promoted=False,
            model_version_id=candidate.id,
            reasons=decision.reasons,
        )

    mm.transition_state(candidate.id, ModelState.APPROVED)
    # Archive the prior production version *before* promoting the new one —
    # see workflow/retraining.py's promotion step for why the order matters.
    if production is not None and production.id != candidate.id:
        mm.transition_state(production.id, ModelState.ARCHIVED)
    mm.transition_state(candidate.id, ModelState.PRODUCTION)
    regsync.sync_production(model_row.name, mlflow_version)

    return PromoteModelResponse(
        promoted=True,
        model_version_id=candidate.id,
        model_version=candidate.version_number,
    )


# ---------------------------------------------------------------------- #
# Write endpoints — dataset / model registration
# ---------------------------------------------------------------------- #


class CreateDatasetRequest(BaseModel):
    name: str
    description: str | None = None


class DatasetOut(BaseModel):
    id: int
    name: str
    created: bool


@router.post("/internal/datasets", response_model=DatasetOut)
def create_dataset(
    request: CreateDatasetRequest,
    dm: DatasetManager = Depends(get_dataset_manager),
) -> DatasetOut:
    """Get or create a dataset by name."""
    existing = dm.get_dataset_by_name(request.name)
    if existing is not None:
        return DatasetOut(id=existing.id, name=existing.name, created=False)
    dataset = dm.create_dataset(request.name, description=request.description)
    return DatasetOut(id=dataset.id, name=dataset.name, created=True)


class CreateDatasetVersionRequest(BaseModel):
    storage_uri: str
    row_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetVersionOut(BaseModel):
    id: int
    dataset_id: int
    version_number: int
    storage_uri: str
    row_count: int
    checksum: str
    schema_hash: str
    created: bool


@router.post(
    "/internal/datasets/{dataset_id}/versions",
    response_model=DatasetVersionOut,
)
def create_dataset_version(
    dataset_id: int,
    request: CreateDatasetVersionRequest,
    dm: DatasetManager = Depends(get_dataset_manager),
) -> DatasetVersionOut:
    """Register a new immutable version of a dataset.

    If ``metadata`` carries a ``content_sha256`` and an existing version of
    this dataset has the same one, that version is returned instead of a new
    one. The framework's own checksum hashes the storage URI plus metadata
    rather than the bytes, so without this a client re-running against
    unchanged data would mint a fresh version every time and break the
    "a version pins the data" promise the readiness engine relies on.
    """
    try:
        dm.get_dataset(dataset_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    content_hash = request.metadata.get("content_sha256")
    if content_hash:
        for existing in dm.list_versions(dataset_id):
            meta = json.loads(existing.metadata_json or "{}")
            if meta.get("content_sha256") == content_hash:
                return DatasetVersionOut(
                    id=existing.id,
                    dataset_id=existing.dataset_id,
                    version_number=existing.version_number,
                    storage_uri=existing.storage_uri,
                    row_count=existing.row_count,
                    checksum=existing.checksum,
                    schema_hash=existing.schema_hash,
                    created=False,
                )

    version = dm.create_version(
        dataset_id=dataset_id,
        storage_uri=request.storage_uri,
        row_count=request.row_count,
        metadata=request.metadata or None,
    )
    return DatasetVersionOut(
        id=version.id,
        dataset_id=version.dataset_id,
        version_number=version.version_number,
        storage_uri=version.storage_uri,
        row_count=version.row_count,
        checksum=version.checksum,
        schema_hash=version.schema_hash,
        created=True,
    )


class CreateModelRequest(BaseModel):
    name: str
    task: str | None = None
    description: str | None = None


class ModelOut(BaseModel):
    id: int
    name: str
    created: bool


@router.post("/internal/models", response_model=ModelOut)
def create_model(
    request: CreateModelRequest,
    mm: ModelManager = Depends(get_model_manager),
) -> ModelOut:
    """Get or create a model by name."""
    existing = mm.get_model_by_name(request.name)
    if existing is not None:
        return ModelOut(id=existing.id, name=existing.name, created=False)
    model = mm.create_model(
        request.name, task=request.task, description=request.description
    )
    return ModelOut(id=model.id, name=model.name, created=True)


# ---------------------------------------------------------------------- #
# POST /internal/readiness/{version_id}
# ---------------------------------------------------------------------- #


class ReadinessRequest(BaseModel):
    policy: dict[str, Any] = Field(default_factory=dict)


class ReadinessOut(BaseModel):
    dataset_version_id: int
    status: str
    is_ready: bool
    checks: dict[str, str]
    reasons: list[str]


@router.post("/internal/readiness/{version_id}", response_model=ReadinessOut)
def evaluate_readiness(
    version_id: int,
    request: ReadinessRequest,
    db: Session = Depends(get_db),
    dm: DatasetManager = Depends(get_dataset_manager),
) -> ReadinessOut:
    """Run the readiness engine against a dataset version and persist it.

    The decision is what gates training, so it has to be made by the
    framework rather than asserted by the caller.
    """
    try:
        version = dm.get_version(version_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    policy = TrainingPolicy.from_dict(request.policy) if request.policy else TrainingPolicy()
    result = ReadinessEngine(db).evaluate(version, policy=policy)
    return ReadinessOut(
        dataset_version_id=version_id,
        status=result.status.value,
        is_ready=bool(result.is_ready),
        checks=result.check_dict(),
        reasons=list(result.reasons),
    )


# ---------------------------------------------------------------------- #
# Training-run lifecycle
# ---------------------------------------------------------------------- #


class CreateTrainingRunRequest(BaseModel):
    dataset_version_id: int
    pipeline_id: str
    trigger_type: str = "API"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TrainingRunOut(BaseModel):
    id: int
    status: str
    pipeline_id: str | None = None
    dataset_version_id: int | None = None
    mlflow_run_id: str | None = None
    execution_id: str | None = None


def _run_out(run: TrainingRun, tm: TrainingManager) -> TrainingRunOut:
    meta = tm.get_run_metadata(run.id)
    status = run.status
    return TrainingRunOut(
        id=run.id,
        status=getattr(status, "value", status),
        pipeline_id=run.pipeline_id,
        dataset_version_id=run.dataset_version_id,
        mlflow_run_id=run.mlflow_run_id,
        execution_id=meta.get("orchestrator_execution_id"),
    )


@router.post("/internal/training-runs", response_model=TrainingRunOut)
def create_training_run(
    request: CreateTrainingRunRequest,
    tm: TrainingManager = Depends(get_training_manager),
) -> TrainingRunOut:
    """Create a PENDING training run against a dataset version."""
    try:
        run = tm.create_run(
            dataset_version_id=request.dataset_version_id,
            pipeline_id=request.pipeline_id,
            trigger_type=request.trigger_type,
            metadata=request.metadata or None,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _run_out(run, tm)


class StartTrainingRunRequest(BaseModel):
    # Defaults come from the app's own environment so a client does not
    # need to know the deployment's internal hostnames.
    experiment_name: str = "fraud-detection"
    dag_id: str | None = None


@router.post("/internal/training-runs/{run_id}/start", response_model=TrainingRunOut)
def start_training_run(
    run_id: int,
    request: StartTrainingRunRequest,
    db: Session = Depends(get_db),
    tm: TrainingManager = Depends(get_training_manager),
) -> TrainingRunOut:
    """Start an MLflow run, trigger the Airflow DAG, mark the run RUNNING.

    This is ``TrainingService.start_run`` exposed over HTTP. It belongs on
    the server rather than in the client for two reasons: the app already
    holds the MLflow and Airflow credentials, and the tracker run must
    exist *before* the DAG reads it out of the run context — a client that
    created it separately would race the scheduler.
    """
    from mlops_framework.orchestration.airflow import AirflowOrchestrator
    from mlops_framework.training.service import TrainingService

    base_url = os.environ.get("AIRFLOW_BASE_URL")
    if not base_url:
        raise HTTPException(
            status_code=503,
            detail="AIRFLOW_BASE_URL is not configured on this deployment",
        )

    tracker = None
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
    if tracking_uri:
        from mlops_framework.tracking.mlflow import MLflowTracker

        # Constructing the tracker reaches out to the tracking server, so it
        # fails the same way a network call does. It sat outside the try
        # below once, and an unreachable MLflow surfaced as an opaque 500
        # instead of naming the dependency that was down.
        try:
            tracker = MLflowTracker(
                tracking_uri=tracking_uri, experiment_name=request.experiment_name
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"MLflow at {tracking_uri} is unreachable: {exc}",
            ) from exc

    run = tm.get_run(run_id)
    if request.dag_id and run.pipeline_id != request.dag_id:
        run.pipeline_id = request.dag_id

    _log.info("run %s: tracker ready, triggering %s", run_id, base_url)
    with AirflowOrchestrator(
        base_url=base_url,
        username=os.environ.get("AIRFLOW_USERNAME", "airflow"),
        password=os.environ.get("AIRFLOW_PASSWORD", "airflow"),
    ) as orchestrator:
        service = TrainingService(
            training_manager=tm, orchestrator=orchestrator, tracker=tracker
        )
        # The DAG reads tracker_run_id and tracking_uri out of the run's
        # metadata via /context, so they must be persisted, not just passed
        # to the orchestrator.
        try:
            execution_id = service.start_run(run_id)
        except Exception as exc:
            _log.exception("run %s: start failed", run_id)
            raise HTTPException(status_code=502, detail=f"start failed: {exc}") from exc
        _log.info("run %s: triggered %s", run_id, execution_id)

    run = tm.get_run(run_id)
    if tracker is not None and run.mlflow_run_id:
        tm.update_metadata(
            run_id,
            {"tracker_run_id": run.mlflow_run_id, "tracking_uri": tracking_uri},
        )
    return _run_out(run, tm)


class FinishTrainingRunRequest(BaseModel):
    status: str = Field(description="SUCCESS or FAILED")
    error_message: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    # Set by mlops_training_pipeline.py's report_status task when the
    # TrainingRun's own metadata carries "owned_by_workflow" — i.e. it was
    # created by RetrainingWorkflow, whose wait_for_completion() polls the
    # orchestrator itself and calls complete_run()/fail_run() when it sees
    # a terminal state. Without this flag both this endpoint and
    # wait_for_completion would race to close out the same run — whichever
    # loses hits an InvalidStatusTransitionError against an already-
    # terminal row. Metadata (metrics/params/artifact_path) is still
    # recorded either way; only the status transition is skipped.
    skip_lifecycle_transition: bool = False


@router.post("/internal/training-runs/{run_id}/finish", response_model=TrainingRunOut)
def finish_training_run(
    run_id: int,
    request: FinishTrainingRunRequest,
    tm: TrainingManager = Depends(get_training_manager),
) -> TrainingRunOut:
    """Close out a run from the orchestrator that executed it.

    Without this the DAG had no way to report back, so a run stayed
    PENDING in the UI forever no matter how the training actually went.
    ``result`` carries the pipeline's own report (metrics, params,
    artifact path) and is stored where the API's run schema reads it.
    """
    wanted = request.status.upper()
    if wanted not in {"SUCCESS", "FAILED"}:
        raise HTTPException(
            status_code=422, detail="status must be SUCCESS or FAILED"
        )
    try:
        run = tm.get_run(run_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        if request.result:
            tm.update_metadata(run_id, {"orchestrator_result": request.result})
        if request.skip_lifecycle_transition:
            run = tm.get_run(run_id)
        else:
            # The lifecycle has no PENDING -> SUCCESS/FAILED edge, by
            # design. A DAG that blew up in its first task still
            # executed, though, so move the run through RUNNING rather
            # than rejecting the report and leaving it PENDING forever —
            # which is the exact failure this endpoint exists to end.
            status = getattr(run.status, "value", run.status)
            if status == "PENDING":
                tm.start_run(run_id)
            if wanted == "SUCCESS":
                run = tm.complete_run(run_id)
            else:
                run = tm.fail_run(run_id, error_message=request.error_message)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _run_out(run, tm)
