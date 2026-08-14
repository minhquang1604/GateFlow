"""Write path into MLflow's own Model Registry.

``mlops_framework.api.mlflow_gateway`` is deliberately read-only (see
its docstring) — this module is its write-side counterpart, the one
place that pushes the framework's own promotion decisions into
MLflow's registry so MLflow's UI and Gateflow's Model Registry page
show the same picture instead of only being *compared* after the fact
(see ``api/routers/mlflow_views.py``'s ``reconcile_model_registry``,
which reads what this module writes).

Lives under ``tracking/`` rather than ``api/`` — alongside
``tracking.mlflow.MLflowTracker``, the run-tracking writer this module
is the model-registry-writing counterpart to — because both of this
module's callers need it, and one of them must not depend on
``mlops_framework.api``:

* ``api/routers/internal.py::promote_model`` — the real Airflow DAG's
  ``register_and_promote`` task, over HTTP. Already in ``api/``.
* ``workflow/retraining.py::RetrainingWorkflow.run()`` — the in-process
  retrain path (e.g. the drift-recovery demo's Phase 2). Part of the
  framework's core layer, imported directly by demo scripts and tests
  with no FastAPI app in the process — it must not import
  ``mlops_framework.api``, which would pull in the whole app (every
  router) just to reach one client constructor.

Design contract: **nothing here ever raises**. The framework's own
Postgres row is the governance decision of record; MLflow being down,
slow, or disagreeing about a stage transition is a degraded *view*
problem, not a reason to fail — or worse, half-apply — a promotion
that already happened in the framework's database. Every function
below returns ``None``/``False`` and logs a warning on any failure.
"""

from __future__ import annotations

import logging
import posixpath

from mlops_framework.tracking.mlflow_client import client_or_reason

_log = logging.getLogger("mlops_framework.tracking.mlflow_registry")

# MLflow 3 deprecated stages in favour of aliases (see mlflow_views.py's
# PRODUCTION_ALIASES), but the deployed tracking server (pinned 2.20.3)
# still shows the Stage column prominently, and stage transitions remain
# supported client-side (just deprecated) — so this sets both, matching
# whichever half of MLflow's UI someone is looking at.
PRODUCTION_ALIAS = "champion"
PRODUCTION_STAGE = "Production"

DEFAULT_ARTIFACT_FILENAME = "model.json"


def _ensure_registered_model(client: object, name: str) -> None:
    """``create_registered_model``, tolerating "already exists"."""
    from mlflow.exceptions import MlflowException

    try:
        client.create_registered_model(name)  # type: ignore[attr-defined]
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_ALREADY_EXISTS":
            raise


def artifact_filename_from_uri(artifact_uri: str | None) -> str:
    """Derive the MLflow-relative artifact filename from whatever a
    caller has on hand.

    Training pipelines report ``artifact_uri`` as the *local* tmp path
    the file was written to before ``mlflow.log_artifact()`` uploaded
    it (see case_studies/fraud_detection/pipelines.py) — but
    ``log_artifact`` with no sub-directory argument always lands the
    file at the run's artifact root under its own basename, so the
    basename of that local path is also the correct relative path for
    a ``runs:/<run_id>/<...>`` registry source URI.
    """
    if not artifact_uri:
        return DEFAULT_ARTIFACT_FILENAME
    return posixpath.basename(artifact_uri) or DEFAULT_ARTIFACT_FILENAME


def sync_candidate(
    model_name: str,
    mlflow_run_id: str | None,
    artifact_filename: str = DEFAULT_ARTIFACT_FILENAME,
) -> str | None:
    """Register a new MLflow Model Version for a just-created CANDIDATE.

    Returns the MLflow version string on success, or ``None`` — MLflow
    unavailable, the run has no such artifact, anything else — without
    raising. The registered model is created on first use (get-or-create,
    same convention as the framework's own dataset/model registration).
    """
    if not mlflow_run_id:
        _log.info("registry sync skipped for %s: no mlflow_run_id on this run", model_name)
        return None

    client, reason = client_or_reason()
    if client is None:
        _log.warning("registry sync skipped for %s: %s", model_name, reason)
        return None

    try:
        _ensure_registered_model(client, model_name)
        source = f"runs:/{mlflow_run_id}/{artifact_filename}"
        mv = client.create_model_version(name=model_name, source=source, run_id=mlflow_run_id)
        _log.info("registered %s v%s (run %s)", model_name, mv.version, mlflow_run_id)
        return str(mv.version)
    except Exception as exc:  # noqa: BLE001 - never fail the caller's promotion
        _log.warning(
            "registry sync: could not register %s (run %s): %s", model_name, mlflow_run_id, exc
        )
        return None


def version_for_run(model_name: str, mlflow_run_id: str | None) -> str | None:
    """Find the MLflow model version registered for ``mlflow_run_id``.

    The promotion paths pass ``sync_production`` the version string
    ``sync_candidate`` just handed back. A rollback has no such handle —
    the version it restores was registered on some earlier run, possibly
    in a different process — so it has to look the version up from the
    run id the framework recorded on its own ModelVersion row.

    Returns ``None`` — MLflow unreachable, the model or run unknown to
    it, no ``mlflow_run_id`` on the framework's row — without raising,
    same contract as everything else here. ``sync_production(name,
    None)`` is then a no-op, so a rollback still completes on the
    framework's side with MLflow's view left stale rather than the
    rollback failing.
    """
    if not mlflow_run_id:
        return None

    client, reason = client_or_reason()
    if client is None:
        _log.warning("registry lookup skipped for %s: %s", model_name, reason)
        return None

    try:
        matches = client.search_model_versions(f"run_id='{mlflow_run_id}'")
    except Exception as exc:  # noqa: BLE001 - never fail the caller's rollback
        _log.warning(
            "registry lookup: could not find a version for %s run %s: %s",
            model_name, mlflow_run_id, exc,
        )
        return None

    for mv in matches:
        if getattr(mv, "name", None) == model_name:
            return str(mv.version)
    _log.info(
        "registry lookup: no %s version registered for run %s", model_name, mlflow_run_id
    )
    return None


def sync_production(model_name: str, version: str | None) -> bool:
    """Mark ``version`` as the one being served on MLflow's side:
    stage=Production (auto-archiving whichever version held it before)
    and the ``champion`` alias.

    Returns True on success, False (logged) otherwise — never raises.
    """
    if not version:
        return False

    client, reason = client_or_reason()
    if client is None:
        _log.warning("registry sync skipped for %s v%s: %s", model_name, version, reason)
        return False

    try:
        client.transition_model_version_stage(
            name=model_name,
            version=version,
            stage=PRODUCTION_STAGE,
            archive_existing_versions=True,
        )
        client.set_registered_model_alias(model_name, PRODUCTION_ALIAS, version)
        _log.info("promoted %s v%s on MLflow (stage=%s, alias=%s)", model_name, version, PRODUCTION_STAGE, PRODUCTION_ALIAS)
        return True
    except Exception as exc:  # noqa: BLE001 - never fail the caller's promotion
        _log.warning("registry sync: could not promote %s v%s: %s", model_name, version, exc)
        return False
