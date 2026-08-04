"""Mount the Management UI on a FastAPI app.

The UI is a collection of plain HTML pages served as ``HTMLResponse``
strings. There is no build step and no templating engine — every page is
a self-contained HTML document that calls the JSON API via ``fetch()`` in
``static/app.js``.

Pages:

* ``/`` and ``/dashboard`` — KPI dashboard
* ``/datasets``, ``/datasets/{id}`` — dataset list and detail
* ``/runs``, ``/runs/{id}`` — run list and detail
* ``/models``, ``/models/{id}`` — model list and detail
* ``/lineage`` — lineage explorer

The static folder (CSS + JS) is mounted at ``/static``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles


_PKG_UI = Path(__file__).parent


def mount_ui(
    app: FastAPI,
    templates_dir: Optional[Path] = None,
) -> None:
    """Mount the Management UI on ``app`` at ``/``.

    Args:
        app: The FastAPI application.
        templates_dir: Optional override for the directory holding the
            ``.html`` pages. Defaults to ``src/mlops_framework/ui/templates``.
    """
    pages = templates_dir or (_PKG_UI / "templates")
    static_dir = _PKG_UI / "static"

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _load(pages, "dashboard.html", "Dashboard")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return _load(pages, "dashboard.html", "Dashboard")

    @app.get("/datasets", response_class=HTMLResponse)
    def datasets() -> str:
        return _load(pages, "datasets.html", "Datasets")

    @app.get("/datasets/{dataset_id}", response_class=HTMLResponse)
    def dataset_detail(dataset_id: int) -> str:
        return _load(pages, "dataset_detail.html", f"Dataset #{dataset_id}")

    @app.get("/runs", response_class=HTMLResponse)
    def runs() -> str:
        return _load(pages, "runs.html", "Training Runs")

    # Registered before /runs/{run_id} so "compare" is not swallowed by the
    # int path converter (which would 422 on a non-numeric segment).
    @app.get("/runs/compare", response_class=HTMLResponse)
    def runs_compare() -> str:
        return _load(pages, "runs_compare.html", "Compare Runs")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: int) -> str:
        return _load(pages, "run_detail.html", f"Run #{run_id}")

    @app.get("/models", response_class=HTMLResponse)
    def models() -> str:
        return _load(pages, "models.html", "Models")

    @app.get("/models/{model_id}", response_class=HTMLResponse)
    def model_detail(model_id: int) -> str:
        return _load(pages, "model_detail.html", f"Model #{model_id}")

    @app.get("/lineage", response_class=HTMLResponse)
    def lineage() -> str:
        return _load(pages, "lineage.html", "Lineage")


def _load(pages: Path, name: str, title: str) -> str:
    """Load a page template, or return a minimal placeholder if missing."""
    target = pages / name
    if target.exists():
        return target.read_text(encoding="utf-8")
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title} - MLOps</title>"
        "<link rel='stylesheet' href='/static/app.css'></head>"
        f"<body><main><h1>{title}</h1>"
        "<p class='muted'>Page not yet implemented.</p>"
        "</main></body></html>"
    )
