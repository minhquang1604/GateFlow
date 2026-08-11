"""FastAPI application factory for the MLOps Management API.

The factory pattern keeps tests deterministic — each test creates a fresh
``app`` with its own database manager, so there is no shared global state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from mlops_framework.api.routers import (
    airflow_views,
    dashboard,
    datasets,
    drift,
    internal,
    lineage,
    mlflow_views,
    models,
    readiness,
    runs,
    schedules,
)


def create_app(
    *,
    title: str = "MLOps Framework Management API",
    version: str = "0.1.0",
    mount_ui: bool = True,
    ui_templates_dir: Optional[Path] = None,
) -> FastAPI:
    """Build a FastAPI app with the management API routers mounted.

    Args:
        title: API title shown in OpenAPI docs.
        version: API version.
        mount_ui: When True (default), serve the static Management UI at ``/``.
        ui_templates_dir: Optional override for the UI templates directory.
            If not provided, the bundled ``src/mlops_framework/ui/templates``
            directory is used.

    Returns:
        A fully-configured :class:`fastapi.FastAPI` instance.
    """
    app = FastAPI(
        title=title,
        version=version,
        description=(
            "HTTP API for the MLOps Framework. Every endpoint is a thin "
            "façade over an existing Week 1-3 manager; no new business "
            "logic is introduced here."
        ),
    )

    # API routers
    app.include_router(dashboard.router, prefix="/api", tags=["dashboard"])
    app.include_router(datasets.router, prefix="/api", tags=["datasets"])
    app.include_router(runs.router, prefix="/api", tags=["runs"])
    app.include_router(models.router, prefix="/api", tags=["models"])
    app.include_router(lineage.router, prefix="/api", tags=["lineage"])
    app.include_router(mlflow_views.router, prefix="/api", tags=["mlflow"])
    app.include_router(airflow_views.router, prefix="/api", tags=["airflow"])
    app.include_router(readiness.router, prefix="/api", tags=["readiness"])
    app.include_router(drift.router, prefix="/api", tags=["drift"])
    app.include_router(schedules.router, prefix="/api", tags=["scheduling"])
    app.include_router(internal.router, prefix="/api", tags=["internal"])

    if mount_ui:
        from mlops_framework.ui.mount import mount_ui as _mount_ui

        _mount_ui(app, templates_dir=ui_templates_dir)

    _warm_mlflow_import(app)
    _start_scheduler(app)

    return app


def _start_scheduler(app: FastAPI) -> None:
    """Fire due Schedules on a background loop — off unless
    ``settings.scheduler_enabled`` is set (see ``config/settings.py``).

    Uses ``asyncio.create_task`` rather than a thread (contrast
    ``_warm_mlflow_import`` above): each tick does real DB and
    training-orchestration work through ``run_due_schedules``, which is
    worth cancelling cleanly on shutdown rather than leaving a daemon
    thread to run mid-tick against a database connection the app is
    tearing down.

    A tick's own failure (a bad cron expression that slipped past
    validation, a training run that raised) is caught and logged, not
    left to kill the loop — one broken schedule must not silently stop
    every other schedule from ever firing again.
    """
    import logging

    from mlops_framework.config.settings import get_settings

    _log = logging.getLogger("mlops_framework.api.scheduler")

    settings = get_settings()
    if not settings.scheduler_enabled:
        return

    @app.on_event("startup")
    async def _start() -> None:
        import asyncio

        async def _loop() -> None:
            from mlops_framework.database.session import DatabaseManager
            from mlops_framework.scheduling.runner import run_due_schedules

            db = DatabaseManager()
            poll_seconds = get_settings().scheduler_poll_seconds
            _log.info("scheduler loop started (poll every %.0fs)", poll_seconds)
            while True:
                try:
                    def _tick() -> list:
                        with db.get_session() as session:
                            return run_due_schedules(
                                session, mlflow_tracking_uri=settings.mlflow_tracking_uri
                            )

                    results = await asyncio.to_thread(_tick)
                    fired = [r for r in results if r.fired]
                    if fired:
                        _log.info(
                            "scheduler tick: fired %d schedule(s): %s",
                            len(fired), [r.schedule_id for r in fired],
                        )
                except Exception:  # noqa: BLE001 - one bad tick must not end the loop
                    _log.exception("scheduler tick failed")
                await asyncio.sleep(poll_seconds)

        app.state.scheduler_task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop() -> None:
        task = getattr(app.state, "scheduler_task", None)
        if task is not None:
            task.cancel()


def _warm_mlflow_import(app: FastAPI) -> None:
    """Import mlflow in the background at boot when it will be needed.

    ``importing mlflow`` costs ~14s of CPU — it drags in pandas, sqlalchemy,
    alembic and more. The framework imports it lazily on purpose so it stays
    usable without MLflow installed, but that put the whole cost inside the
    first request that needed it. On a container reserved 320 CPU units
    (0.31 vCPU) that stretched past the 60s timeout on ECS Service Connect's
    ingress listener, so ``POST /internal/training-runs/{id}/start`` was cut
    off mid-import every time and the caller saw an empty reply.

    Warming it on a daemon thread keeps startup itself fast — blocking here
    would eat into the health check's start period — and by the time a
    client can reach ``/start`` the module is already in ``sys.modules``.
    Failures are ignored: this is a cache warm, and the endpoint still
    reports a missing or unreachable MLflow properly on its own.
    """
    import os

    if not os.environ.get("MLFLOW_TRACKING_URI"):
        return

    @app.on_event("startup")
    def _warm() -> None:
        import threading

        def _load() -> None:
            try:
                import mlflow  # noqa: F401
            except Exception:  # pragma: no cover - environment dependent
                pass

        threading.Thread(target=_load, name="mlflow-warm", daemon=True).start()
