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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.api.mlflow_gateway import client_or_reason, panel
from mlops_framework.api.schemas import ExternalPanel
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.database.models.training_run import TrainingRun

router = APIRouter()

# Aliases that mean "this is the version being served". MLflow 3 deprecated
# stages in favour of aliases, and an alias is just a name someone chose, so
# there is no authoritative list — these are the conventional ones. Anything
# outside the set is still reported, just not read as "production".
PRODUCTION_ALIASES = {"champion", "production", "prod"}

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
    order_by: str | None = Query(
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


def _artifacts_panel(mlflow_run_id: str, safe: str) -> ExternalPanel:
    """List one directory of a run's artifacts. Shared by both the
    framework-run-scoped and raw-mlflow-run-id-scoped routes below."""

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


@router.get("/training-runs/{run_id}/artifacts", response_model=ExternalPanel)
def list_run_artifacts(
    run_id: int,
    path: str = Query(default="", description="Directory within the run's artifacts"),
    db: Session = Depends(get_db),
) -> ExternalPanel:
    """List one directory of a run's artifacts, by framework run id."""
    mlflow_run_id = _mlflow_run_id(db, run_id)
    return _artifacts_panel(mlflow_run_id, _safe_artifact_path(path))


@router.get("/mlflow/runs/{mlflow_run_id}/artifacts", response_model=ExternalPanel)
def list_run_artifacts_by_mlflow_id(
    mlflow_run_id: str,
    path: str = Query(default="", description="Directory within the run's artifacts"),
) -> ExternalPanel:
    """List one directory of a run's artifacts, by raw MLflow run id —
    for a run the framework has no TrainingRun row for at all (a sweep's
    child run, a run started outside this framework...). See
    ``get_run_summary`` for why this sibling route exists."""
    return _artifacts_panel(mlflow_run_id, _safe_artifact_path(path))


def _artifact_file_response(mlflow_run_id: str, safe: str) -> FileResponse:
    """Stream a single artifact through the app.

    Not an :class:`ExternalPanel`: the browser consumes this directly as an
    ``<img>`` source or a download, so it answers with the bytes or an HTTP
    error rather than a wrapped payload. Shared by both artifact-download
    routes below.
    """
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


@router.get("/training-runs/{run_id}/artifacts/raw")
def get_run_artifact(
    run_id: int,
    path: str = Query(..., description="File within the run's artifacts"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Stream a single artifact through the app, by framework run id."""
    mlflow_run_id = _mlflow_run_id(db, run_id)
    return _artifact_file_response(mlflow_run_id, _safe_artifact_path(path))


@router.get("/mlflow/runs/{mlflow_run_id}/artifacts/raw")
def get_run_artifact_by_mlflow_id(
    mlflow_run_id: str,
    path: str = Query(..., description="File within the run's artifacts"),
) -> FileResponse:
    """Stream a single artifact through the app, by raw MLflow run id."""
    return _artifact_file_response(mlflow_run_id, _safe_artifact_path(path))


# ---------------------------------------------------------------------- #
# Model signature and environment
# ---------------------------------------------------------------------- #


def _find_mlmodel_in_run(client: Any, mlflow_run_id: str) -> str | None:
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
    try:
        results = list(search(experiment_ids=[experiment_id]))
    except Exception:  # noqa: BLE001 - a 2.x server 404s this 3.x-only
        # endpoint outright rather than answering "zero results"; either
        # way there is no logged-model entity to describe, same as the
        # run-artifact branch above finding nothing.
        return {}, "", "none"

    for model in results:
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


def _model_info_panel(mlflow_run_id: str) -> ExternalPanel:
    """Return the logged model's signature, flavors and environment.
    Shared by both model-info routes below."""

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


@router.get("/training-runs/{run_id}/model-info", response_model=ExternalPanel)
def get_run_model_info(
    run_id: int,
    db: Session = Depends(get_db),
) -> ExternalPanel:
    """Return the logged model's signature, flavors and environment, by
    framework run id."""
    return _model_info_panel(_mlflow_run_id(db, run_id))


@router.get("/mlflow/runs/{mlflow_run_id}/model-info", response_model=ExternalPanel)
def get_run_model_info_by_mlflow_id(mlflow_run_id: str) -> ExternalPanel:
    """Return the logged model's signature, flavors and environment, by
    raw MLflow run id."""
    return _model_info_panel(mlflow_run_id)


# ---------------------------------------------------------------------- #
# Model registry reconciliation
# ---------------------------------------------------------------------- #


def _alias_map(client: Any, name: str) -> dict[str, list[str]]:
    """Return ``{version: [alias, ...]}`` for a registered model.

    MLflow 3 hangs aliases off the *registered model* as a
    ``{alias: version}`` map, and leaves ``ModelVersion.aliases`` empty, so
    reading a version's aliases means inverting that map rather than asking
    the version.
    """
    out: dict[str, list[str]] = {}
    try:
        registered = client.get_registered_model(name)
    except Exception:  # noqa: BLE001 - a missing model is not an error here
        return out
    for alias, version in (getattr(registered, "aliases", None) or {}).items():
        out.setdefault(str(version), []).append(str(alias))
    return out


def _mlflow_says_production(stage: str, aliases: list[str]) -> bool:
    """Whether MLflow considers a version the one being served."""
    if str(stage).strip().lower() == "production":
        return True
    return any(a.lower() in PRODUCTION_ALIASES for a in aliases)


@router.get("/mlflow/registered-models", response_model=ExternalPanel)
def list_registered_models() -> ExternalPanel:
    """List the tracking server's registered models and their aliases."""

    def query(client: Any) -> dict[str, Any]:
        models = []
        for m in client.search_registered_models():
            aliases = getattr(m, "aliases", None) or {}
            models.append(
                {
                    "name": m.name,
                    "description": m.description or "",
                    "tags": dict(getattr(m, "tags", None) or {}),
                    "aliases": {str(k): str(v) for k, v in aliases.items()},
                    "latest_versions": [
                        {"version": str(v.version), "stage": str(v.current_stage)}
                        for v in (getattr(m, "latest_versions", None) or [])
                    ],
                }
            )
        return {"models": models}

    return panel(query)


@router.get("/models/{model_id}/registry-reconciliation", response_model=ExternalPanel)
def reconcile_model_registry(
    model_id: int,
    db: Session = Depends(get_db),
) -> ExternalPanel:
    """Compare this framework's model registry against MLflow's.

    The framework promotes versions in its own Postgres table; MLflow keeps
    a registry of its own. Nothing reconciles them, so the two can disagree
    silently — a version marked PRODUCTION here with no alias in MLflow
    means whatever is actually being served was never blessed on one of the
    two sides. This endpoint puts both columns next to each other and
    flags the rows where they differ.

    The join key is the MLflow run id, which both sides record:
    ``ModelVersion.mlflow_run_id`` here, ``ModelVersion.run_id`` there.
    """
    model = db.get(ModelRow, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    rows = list(
        db.execute(
            select(ModelVersion)
            .where(ModelVersion.model_id == model_id)
            .order_by(ModelVersion.version_number)
        )
        .scalars()
        .all()
    )

    def query(client: Any) -> dict[str, Any]:
        # Look the registry up by name first — that is the intended
        # correspondence — but fall back to whatever the versions' run ids
        # resolve to, so a differently-named registered model still gets
        # reconciled instead of silently reading as "absent".
        mlflow_versions: dict[str, Any] = {}
        registry_names: set[str] = set()
        try:
            for mv in client.search_model_versions(f"name='{model.name}'"):
                if mv.run_id:
                    mlflow_versions[mv.run_id] = mv
                    registry_names.add(mv.name)
        except Exception:  # noqa: BLE001 - no such registered model
            pass
        for row in rows:
            if row.mlflow_run_id and row.mlflow_run_id not in mlflow_versions:
                try:
                    found = client.search_model_versions(f"run_id='{row.mlflow_run_id}'")
                except Exception:  # noqa: BLE001
                    continue
                for mv in found:
                    mlflow_versions[mv.run_id] = mv
                    registry_names.add(mv.name)

        aliases_by_name = {n: _alias_map(client, n) for n in registry_names}

        compared = []
        drift_count = 0
        for row in rows:
            mv = mlflow_versions.get(row.mlflow_run_id or "")
            fw_production = row.state == ModelState.PRODUCTION.value
            entry: dict[str, Any] = {
                "framework_version_id": row.id,
                "framework_version_number": row.version_number,
                "framework_state": row.state,
                "mlflow_run_id": row.mlflow_run_id,
                "in_mlflow_registry": mv is not None,
                "mlflow_name": None,
                "mlflow_version": None,
                "mlflow_stage": None,
                "mlflow_aliases": [],
                "mlflow_status": None,
            }
            if mv is None:
                # Only a promoted version that MLflow has never heard of is
                # worth flagging; a CANDIDATE that was never registered is
                # the expected state, not a discrepancy.
                entry["drift"] = fw_production
                entry["drift_reason"] = (
                    "framework has this version in PRODUCTION but MLflow's "
                    "registry has no entry for its run"
                    if fw_production
                    else None
                )
            else:
                aliases = aliases_by_name.get(mv.name, {}).get(str(mv.version), [])
                ml_production = _mlflow_says_production(mv.current_stage, aliases)
                entry.update(
                    {
                        "mlflow_name": mv.name,
                        "mlflow_version": str(mv.version),
                        "mlflow_stage": str(mv.current_stage),
                        "mlflow_aliases": aliases,
                        "mlflow_status": str(getattr(mv, "status", "")),
                    }
                )
                entry["drift"] = fw_production != ml_production
                if entry["drift"]:
                    entry["drift_reason"] = (
                        "framework says PRODUCTION, MLflow has no production "
                        "stage or alias"
                        if fw_production
                        else "MLflow marks this version as serving, the "
                        "framework does not"
                    )
                else:
                    entry["drift_reason"] = None
            drift_count += 1 if entry["drift"] else 0
            compared.append(entry)

        known_runs = {r.mlflow_run_id for r in rows if r.mlflow_run_id}
        orphans = [
            {
                "mlflow_name": mv.name,
                "mlflow_version": str(mv.version),
                "run_id": mv.run_id,
                "stage": str(mv.current_stage),
                "aliases": aliases_by_name.get(mv.name, {}).get(str(mv.version), []),
            }
            for run_id, mv in mlflow_versions.items()
            if run_id not in known_runs
        ]

        return {
            "model_name": model.name,
            "registry_names": sorted(registry_names),
            "versions": compared,
            "drift_count": drift_count,
            "mlflow_only": orphans,
        }

    return panel(query)


# ---------------------------------------------------------------------- #
# Nested runs
# ---------------------------------------------------------------------- #


def _nested_runs_panel(mlflow_run_id: str) -> ExternalPanel:
    """Return this run's place in an MLflow parent/child sweep.

    A hyperparameter sweep logs one parent run and a child per trial, tied
    together by the ``mlflow.parentRunId`` tag. Without reading that tag the
    console shows a sweep as a flat list of unrelated runs. Shared by both
    nested-run routes below.
    """

    def query(client: Any) -> dict[str, Any]:
        this = client.get_run(mlflow_run_id)
        experiment_id = this.info.experiment_id
        parent_id = (this.data.tags or {}).get("mlflow.parentRunId")

        def summarise(run: Any) -> dict[str, Any]:
            return {
                "run_id": run.info.run_id,
                "run_name": run.info.run_name,
                "status": run.info.status,
                "start_time": run.info.start_time,
                "metrics": dict(run.data.metrics),
                "params": dict(run.data.params),
                "is_self": run.info.run_id == mlflow_run_id,
            }

        # Children of whichever run heads this tree: the parent if this run
        # has one, otherwise this run itself.
        root_id = parent_id or mlflow_run_id
        children = client.search_runs(
            experiment_ids=[experiment_id],
            filter_string=f"tags.mlflow.parentRunId = '{root_id}'",
            order_by=["attributes.start_time ASC"],
            max_results=200,
        )

        parent = None
        if parent_id:
            try:
                parent = summarise(client.get_run(parent_id))
            except Exception:  # noqa: BLE001 - parent may have been deleted
                parent = {"run_id": parent_id, "run_name": None, "status": "UNKNOWN"}
        elif children:
            parent = summarise(this)

        return {
            "is_child": parent_id is not None,
            "is_parent": parent_id is None and bool(children),
            "root_run_id": root_id,
            "parent": parent,
            "children": [summarise(r) for r in children],
        }

    return panel(query)


@router.get("/training-runs/{run_id}/nested", response_model=ExternalPanel)
def get_nested_runs(
    run_id: int,
    db: Session = Depends(get_db),
) -> ExternalPanel:
    """This run's place in an MLflow parent/child sweep, by framework run id."""
    return _nested_runs_panel(_mlflow_run_id(db, run_id))


@router.get("/mlflow/runs/{mlflow_run_id}/nested", response_model=ExternalPanel)
def get_nested_runs_by_mlflow_id(mlflow_run_id: str) -> ExternalPanel:
    """This run's place in an MLflow parent/child sweep, by raw MLflow run id."""
    return _nested_runs_panel(mlflow_run_id)
