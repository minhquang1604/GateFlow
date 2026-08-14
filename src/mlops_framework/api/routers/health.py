"""Liveness and readiness probes.

Mounted at the root, not under ``/api``: these answer questions about
the *process*, not about the framework's domain, and the things that
ask them — a container runtime, a load balancer, ``docker compose``'s
``healthcheck`` — expect a short fixed path. Every other service in the
stack already publishes one (``mlflow``'s and ``airflow``'s ``/health``,
the ServingBridge's ``/healthz``); the app was the only one that did
not, so compose could tell "the container is up" from nothing else, and
``deploy.yml`` had only ECS's ``runningCount`` to go on — which turns
green while uvicorn is still importing.

Two endpoints, because they answer different questions and a caller
that conflates them makes bad decisions:

* ``/health`` — *liveness*. Is this process running and serving? Touches
  nothing else. A dependency being down must never fail this, or an
  orchestrator restarts a healthy app for someone else's outage and
  turns a degraded read into an outage of its own.
* ``/ready`` — *readiness*. Can this process actually do its job right
  now? Pings the database, because every domain endpoint needs it.
  MLflow and Airflow are deliberately *not* checked: the console
  degrades a panel when they are unreachable rather than failing, so
  they are not preconditions for serving traffic. ``/api/settings``
  already reports their live reachability for an operator who wants it.

Never gated by ``require_write_token`` — a probe cannot carry a
credential, and neither endpoint discloses anything a caller could not
learn by watching whether requests succeed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import text

from mlops_framework.api.deps import get_db_manager_dep
from mlops_framework.database.session import DatabaseManager

_log = logging.getLogger("mlops_framework.api.health")

router = APIRouter()


class HealthOut(BaseModel):
    status: str


class ReadyOut(BaseModel):
    status: str
    database: str
    detail: str | None = None


@router.get("/health", response_model=HealthOut, tags=["health"])
def health() -> HealthOut:
    """Liveness: the process is up and routing requests."""
    return HealthOut(status="ok")


@router.get("/ready", response_model=ReadyOut, tags=["health"])
def ready(
    response: Response,
    db: DatabaseManager = Depends(get_db_manager_dep),
) -> ReadyOut:
    """Readiness: the database answers.

    503 when it does not, so a load balancer stops sending traffic here
    while still leaving the container alone (see ``/health``).

    Uses ``get_db_manager_dep`` and opens its own short-lived session
    rather than depending on ``get_db``: a probe must report a broken
    database as a 503 body it controls, and ``get_db`` would raise on
    the way in and turn that into a 500 from the framework instead. It
    is also the dependency ``app.dependency_overrides`` replaces, so
    tests probe their own database rather than whatever ``DATABASE_URL``
    happens to point at.
    """
    try:
        with db.get_session() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
        _log.warning("readiness probe failed: %s", exc)
        response.status_code = 503
        return ReadyOut(status="not ready", database="unreachable", detail=str(exc))
    return ReadyOut(status="ready", database="ok")
