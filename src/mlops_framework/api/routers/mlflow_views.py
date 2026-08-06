"""MLflow-backed read-only views.

The framework's own tables answer "what did we run and what did we
promote". MLflow holds the rest of the story — the experiment a run
belongs to, the artifacts it produced, the git commit that produced it,
and the signature of the model that came out. These endpoints surface
that without making the console depend on MLflow being up: every one of
them returns :class:`ExternalPanel`, so a missing or unreachable tracking
server degrades a card instead of failing a page.

Module name is ``mlflow_views`` rather than ``mlflow`` so a reader is
never left wondering whether an import refers to this module or the
tracking library.
"""

from __future__ import annotations

import json
import mimetypes
import posixpath
import tempfile
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.mlflow_gateway import client_or_reason, panel
from mlops_framework.api.schemas import ExternalPanel
from mlops_framework.database.models.training_run import TrainingRun

router = APIRouter()

# Artifacts are proxied through this process because the store (S3 in the
# deployed stack) is not reachable from the browser. That makes size a
# real concern: a serialized model can be hundreds of MB, and streaming
# one through the app would tie up a worker and the container's memory.
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024


# ---------------------------------------------------------------------- #
# Experiments
# ---------------------------------------------------------------------- #


@router.get("/mlflow/experiments", response_model=ExternalPanel)
def list_experiments() -> ExternalPanel:
    """List experiments known to the tracking server."""

    def query(client: Any) -> dict[str, Any]:
        experiments = [
            {
                "experiment_id": e.experiment_id,
                "name": e.name,
                "lifecycle_stage": e.lifecycle_stage,
                "artifact_location": e.artifact_location,
                "creation_time": e.creation_time,
                "last_update_time": e.last_update_time,
                "tags": dict(e.tags or {}),
            }
            for e in client.search_experiments()
        ]
        return {"experiments": experiments}

    return panel(query)


@router.get(
    "/mlflow/experiments/{experiment_id}/runs", response_model=ExternalPanel
)
def experiment_runs(
    experiment_id: str,
    order_by: Optional[str] = Query(
        default=None,
        description="Metric to rank by, e.g. 'average_precision'. "
        "Defaults to start time.",
    ),
    direction: str = Query(default="DESC", pattern="^(?i)(asc|desc)$"),
    filter_string: str = Query(
        default="", description="MLflow filter expression, e.g. \"params.max_depth = '6'\""
    ),
    limit: int = Query(default=50, ge=1, le=500),
) -> ExternalPanel:
    """Rank an experiment's runs — the leaderboard the framework lacked.

    ``search_runs`` does the ordering server-side, so this stays one call
    no matter how many runs the experiment holds.
    """
    order = (
        [f"metrics.{order_by} {direction.upper()}"]
        if order_by
        else ["attributes.start_time DESC"]
    )

    def query(client: Any) -> dict[str, Any]:
        runs = client.search_runs(
            experiment_ids=[experiment_id],
            filter_string=filter_string or "",
            order_by=order,
            max_results=limit,
        )
        return {
            "experiment_id": experiment_id,
            "order_by": order[0],
            "runs": [
                {
                    "run_id": r.info.run_id,
                    "run_name": r.info.run_name,
                    "status": r.info.status,
                    "start_time": r.info.start_time,
                    "end_time": r.info.end_time,
                    "metrics": dict(r.data.metrics),
                    "params": dict(r.data.params),
                    "tags": {
                        k: v
                        for k, v in (r.data.tags or {}).items()
                        if not k.startswith("mlflow.log-model")
                    },
                }
                for r in runs
            ],
        }

    return panel(query)


# ---------------------------------------------------------------------- #
# Artifacts
# ---------------------------------------------------------------------- #


def _mlflow_run_id(db: Session, run_id: int) -> str:
    """Resolve a framework run to its MLflow run id, or 404/409."""
    run = db.get(TrainingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"TrainingRun {run_id} not found")
    if not run.mlflow_run_id:
        raise HTTPException(
            status_code=409, detail=f"TrainingRun {run_id} has no MLflow run id"
        )
    return str(run.mlflow_run_id)


def _safe_artifact_path(path: str) -> str:
    """Validate a client-supplied artifact path.

    The path arrives from the browser and is handed to MLflow, which for a
    local store maps it onto the filesystem. Absolute paths and ``..``
    segments must never reach it. ``posixpath.normpath`` collapses the
    path first so encodings like ``a/../../b`` cannot slip through a naive
    substring check.

    Raises:
        HTTPException: 400 if the path escapes the artifact root.
    """
    if not path:
        return ""
    if "\\" in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="artifact path must be relative")
    normalised = posixpath.normpath(path)
    if normalised.startswith("..") or normalised.startswith("/"):
        raise HTTPException(status_code=400, detail="artifact path escapes the run")
    if any(part == ".." for part in normalised.split("/")):
        raise HTTPException(status_code=400, detail="artifact path escapes the run")
    return "" if normalised == "." else normalised


@router.get("/training-runs/{run_id}/artifacts", response_model=ExternalPanel)
def list_run_artifacts(
    run_id: int,
    path: str = Query(default="", description="Directory within the run's artifacts"),
    db: Session = Depends(get_db),
) -> ExternalPanel:
    """List one directory of a run's artifacts."""
    mlflow_run_id = _mlflow_run_id(db, run_id)
    safe = _safe_artifact_path(path)

    def query(client: Any) -> dict[str, Any]:
        entries = client.list_artifacts(mlflow_run_id, safe or None)
        return {
            "path": safe,
            "entries": sorted(
                (
                    {
                        "path": f.path,
                        "name": posixpath.basename(f.path.rstrip("/")),
                        "is_dir": bool(f.is_dir),
                        "file_size": f.file_size,
                    }
                    for f in entries
                ),
                key=lambda e: (not e["is_dir"], e["name"].lower()),
            ),
        }

    return panel(query)


@router.get("/training-runs/{run_id}/artifacts/raw")
def get_run_artifact(
    run_id: int,
    path: str = Query(..., description="File within the run's artifacts"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Stream a single artifact through the app.

    Not an :class:`ExternalPanel`: the browser consumes this directly as an
    ``<img>`` source or a download, so it answers with the bytes or an HTTP
    error rather than a wrapped payload.
    """
    mlflow_run_id = _mlflow_run_id(db, run_id)
    safe = _safe_artifact_path(path)
    if not safe:
        raise HTTPException(status_code=400, detail="path is required")

    client, reason = client_or_reason()
    if client is None:
        raise HTTPException(status_code=503, detail=reason)

    # The temp dir is intentionally not cleaned up here: FileResponse reads
    # it after this function returns. It lands under the system temp dir,
    # which the OS reclaims.
    dst = tempfile.mkdtemp(prefix="mlops-artifact-")
    try:
        local = client.download_artifacts(mlflow_run_id, safe, dst)
    except Exception as exc:  # noqa: BLE001 - surface as a clean 404
        raise HTTPException(
            status_code=404, detail=f"artifact {safe!r} not available: {exc}"
        ) from exc

    resolved = Path(local).resolve()
    # Second line of defence behind _safe_artifact_path: whatever MLflow
    # handed back must still sit inside the directory we chose.
    if not resolved.is_relative_to(Path(dst).resolve()):
        raise HTTPException(status_code=400, detail="artifact path escapes the run")
    if resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"{safe!r} is a directory")

    size = resolved.stat().st_size
    if size > MAX_ARTIFACT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"artifact is {size} bytes, over the {MAX_ARTIFACT_BYTES} byte "
                "limit this console will proxy; fetch it from MLflow directly"
            ),
        )

    media_type, _ = mimetypes.guess_type(resolved.name)
    return FileResponse(
        resolved,
        media_type=media_type or "application/octet-stream",
        filename=resolved.name,
    )


# ---------------------------------------------------------------------- #
# Model signature and environment
# ---------------------------------------------------------------------- #


def _find_mlmodel_in_run(client: Any, mlflow_run_id: str) -> Optional[str]:
    """Locate an ``MLmodel`` descriptor among a run's own artifacts.

    This is the MLflow 2.x layout, which the deployed tracking server
    (pinned to 2.20.3) still uses: ``log_model`` writes the model under the
    run's artifact tree. Convention puts it at ``model/MLmodel``, but the
    directory is named by whoever logged it, so search one level down
    rather than assuming.
    """
    for entry in client.list_artifacts(mlflow_run_id):
        if not entry.is_dir:
            continue
        for child in client.list_artifacts(mlflow_run_id, entry.path):
            if posixpath.basename(child.path) == "MLmodel":
                return child.path
    return None


def _read_yaml(local_path: str) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(Path(local_path).read_text(encoding="utf-8")) or {}


def _mlmodel_spec(client: Any, mlflow_run_id: str) -> tuple[dict[str, Any], str, str]:
    """Fetch a run's ``MLmodel`` descriptor from wherever this MLflow keeps it.

    MLflow 3 changed where that is. Under 2.x a logged model is part of the
    run's artifacts; under 3.x it is a separate *LoggedModel* entity and
    ``list_artifacts(run_id)`` comes back empty. Both layouts are live here
    — the app image installs an unpinned ``mlflow>=2.20.0`` (so 3.x) while
    the deployed server is 2.20.3 — so both are handled.

    Returns:
        ``(spec, location, layout)``. ``spec`` is empty when no model was
        logged through ``log_model`` at all.
    """
    descriptor = _find_mlmodel_in_run(client, mlflow_run_id)
    if descriptor is not None:
        dst = tempfile.mkdtemp(prefix="mlops-mlmodel-")
        local = client.download_artifacts(mlflow_run_id, descriptor, dst)
        return _read_yaml(local), posixpath.dirname(descriptor), "run-artifact"

    search = getattr(client, "search_logged_models", None)
    if search is None:  # pragma: no cover - client older than 3.x
        return {}, "", "none"

    experiment_id = client.get_run(mlflow_run_id).info.experiment_id
    for model in search(experiment_ids=[experiment_id]):
        if getattr(model, "source_run_id", None) != mlflow_run_id:
            continue
        # Not model.model_uri: that is a `models:/...` URI which resolves
        # through the *registry*, and an unregistered logged model 404s
        # there. artifact_location addresses the files directly.
        import mlflow

        uri = str(model.artifact_location).rstrip("/") + "/MLmodel"
        local = mlflow.artifacts.download_artifacts(
            artifact_uri=uri, dst_path=tempfile.mkdtemp(prefix="mlops-mlmodel-")
        )
        return _read_yaml(local), str(model.artifact_location), "logged-model"

    return {}, "", "none"


@router.get("/training-runs/{run_id}/model-info", response_model=ExternalPanel)
def get_run_model_info(
    run_id: int,
    db: Session = Depends(get_db),
) -> ExternalPanel:
    """Return the logged model's signature, flavors and environment."""
    mlflow_run_id = _mlflow_run_id(db, run_id)

    def query(client: Any) -> dict[str, Any]:
        spec, location, layout = _mlmodel_spec(client, mlflow_run_id)
        if not spec:
            return {
                "found": False,
                # Said plainly because it is the common case in this
                # framework: training logs the serialized model with
                # log_artifact(), which produces no MLmodel descriptor and
                # therefore no signature. Only mlflow.<flavor>.log_model()
                # records one.
                "note": (
                    "No MLmodel descriptor for this run. A signature is only "
                    "recorded when the model is logged with "
                    "mlflow.<flavor>.log_model(); log_artifact() stores the "
                    "raw file without one."
                ),
            }

        # signature.inputs/outputs are JSON *strings* nested inside the
        # YAML, so they need a second decode before the UI can table them.
        signature = dict(spec.get("signature") or {})
        for key in ("inputs", "outputs", "params"):
            raw = signature.get(key)
            if isinstance(raw, str):
                try:
                    signature[key] = json.loads(raw)
                except json.JSONDecodeError:
                    pass  # leave the raw string; the UI renders it as text

        flavors = spec.get("flavors") or {}
        return {
            "found": True,
            "layout": layout,
            "artifact_path": location,
            "flavors": sorted(flavors.keys()),
            "flavor_detail": {
                name: {
                    k: v
                    for k, v in (detail or {}).items()
                    if isinstance(v, (str, int, float, bool))
                }
                for name, detail in flavors.items()
            },
            "signature": signature,
            "model_uuid": spec.get("model_uuid"),
            "mlflow_version": spec.get("mlflow_version"),
            "utc_time_created": spec.get("utc_time_created"),
        }

    return panel(query)
