"""FastAPI application factory for the MLOps Management API.

The factory pattern keeps tests deterministic — each test creates a fresh
``app`` with its own database manager, so there is no shared global state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI

from mlops_framework.api.routers import (
    dashboard,
    datasets,
    lineage,
    models,
    readiness,
    runs,
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
    app.include_router(readiness.router, prefix="/api", tags=["readiness"])

    if mount_ui:
        from mlops_framework.ui.mount import mount_ui as _mount_ui

        _mount_ui(app, templates_dir=ui_templates_dir)

    return app
