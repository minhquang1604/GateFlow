"""Mount the Gateflow Management UI on a FastAPI app.

There is no build step and no templating engine. The console shell — top
navigation, left side navigation, content frame — is composed here, and
each file under ``templates/`` is a *fragment*: the inner content of
``<main>`` plus the ``<script>`` line that boots its page. Keeping the
shell in one place is what makes the navigation consistent; before this,
every page carried its own copy of the header and they drifted.

Layout (AWS Console style):

    +--------------------------------------------------+
    |  top navigation (dark, sticky)                    |
    +-------------+------------------------------------+
    |  side nav   |  main content                      |
    |  (scrolls   |  (scrolls with the page)           |
    |   on its    |                                    |
    |   own)      |                                    |
    +-------------+------------------------------------+

Pages:

* ``/`` and ``/dashboard`` — KPI dashboard
* ``/datasets``, ``/datasets/{id}`` — dataset list and detail
* ``/runs``, ``/runs/{id}``, ``/runs/compare`` — runs, detail, compare.
  ``/runs?experiment={id}`` switches to MLflow's own ranked view of
  that experiment — this used to be the separate ``/experiments``
  pages, which now just redirect here.
* ``/mlflow-runs/{mlflow_run_id}`` — run detail for a run with no
  framework TrainingRun row, reached from ``/runs?experiment={id}``
* ``/models``, ``/models/{id}`` — model list and detail
* ``/schedules`` — cron-triggered automatic retraining (create, edit,
  run-now); see ``scheduling/runner.py``
* ``/lineage`` — lineage explorer
* ``/settings`` — effective MLflow/Airflow/database config, secrets
  masked, plus a live reachability ping for MLflow and Airflow
* ``/activity`` — the audit trail: who (or what) triggered a schedule
  or model-promotion action, and when — see ``audit/manager.py``

The static folder (CSS + JS) is mounted at ``/static``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

_PKG_UI = Path(__file__).parent

BRAND = "Gateflow"


def _asset_version(path: Path) -> str:
    """Short content hash for a cache-busting ``?v=`` query string.

    Browsers cache a *favicon* far more stubbornly than they cache a
    normal `<link rel="stylesheet">` — editing favicon.svg/.png in place
    at the same URL was invisible in an already-open tab (and often a
    fresh one) with no way for the server to tell the browser otherwise,
    since there is no build step handing out a new filename per change.
    Hashing the file once at import time and appending it to the href
    does that job: the URL itself changes whenever the artwork does.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8] if path.exists() else "0"


_FAVICON_VERSION = _asset_version(_PKG_UI / "static" / "favicon.svg")
# Same problem, same fix, for the CSS and JS the console actually runs —
# a code change here is invisible in an already-open tab (and often a
# fresh load too) without a version bump in the URL, since app.css/app.js
# are otherwise requested from the exact same path on every deploy.
_CSS_VERSION = _asset_version(_PKG_UI / "static" / "app.css")
_JS_VERSION = _asset_version(_PKG_UI / "static" / "app.js")

# Inline 16px stroke icons. Inlined rather than linked so the console has
# no external asset to fetch and renders identically offline.
_ICONS: dict[str, str] = {
    "dashboard": (
        '<rect x="2" y="2" width="5.5" height="5.5" rx="1"/>'
        '<rect x="10.5" y="2" width="5.5" height="5.5" rx="1"/>'
        '<rect x="2" y="10.5" width="5.5" height="5.5" rx="1"/>'
        '<rect x="10.5" y="10.5" width="5.5" height="5.5" rx="1"/>'
    ),
    "datasets": (
        '<ellipse cx="9" cy="4" rx="6.5" ry="2.4"/>'
        '<path d="M2.5 4v10c0 1.3 2.9 2.4 6.5 2.4s6.5-1.1 6.5-2.4V4"/>'
        '<path d="M2.5 9c0 1.3 2.9 2.4 6.5 2.4s6.5-1.1 6.5-2.4"/>'
    ),
    "runs": (
        '<path d="M1.5 12.5l3.5-4.5 3 3 4-6 4.5 7.5"/>'
        '<circle cx="5" cy="8" r="1.3"/><circle cx="8" cy="11" r="1.3"/>'
    ),
    "models": (
        '<path d="M9 1.8l6.5 3.6v7.2L9 16.2 2.5 12.6V5.4z"/>'
        '<path d="M2.5 5.4L9 9l6.5-3.6M9 9v7.2"/>'
    ),
    "scheduling": (
        '<circle cx="9" cy="9.5" r="6.5"/><path d="M9 6v3.5l2.5 1.8"/>'
        '<path d="M6.2 2.2 3.5 4M11.8 2.2 14.5 4"/>'
    ),
    "lineage": (
        '<circle cx="3.5" cy="4" r="2"/><circle cx="3.5" cy="14" r="2"/>'
        '<circle cx="14.5" cy="9" r="2"/><path d="M5.5 4.8l7 3.4M5.5 13.2l7-3.4"/>'
    ),
    "pipelines": (
        '<circle cx="3.2" cy="3.2" r="1.7"/><circle cx="14.8" cy="3.2" r="1.7"/>'
        '<circle cx="9" cy="14.8" r="1.7"/>'
        '<path d="M4.7 4.3L8 13.4M13.3 4.3L10 13.4M4.9 3.2h9.2"/>'
    ),
    # A ring of 8 short teeth, not rays from the centre — the previous
    # version read as a sun/asterisk (worse still, right next to a
    # theme toggle that was *also* an unclear circle) rather than a
    # gear. Teeth are rects rotated around the centre rather than a
    # hand-plotted path, so the spacing is exactly even.
    "settings": (
        '<circle cx="9" cy="9" r="4.3"/><circle cx="9" cy="9" r="1.6"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(0 9 9)"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(45 9 9)"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(90 9 9)"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(135 9 9)"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(180 9 9)"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(225 9 9)"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(270 9 9)"/>'
        '<rect x="7.95" y="2.15" width="2.1" height="1.7" rx="0.35" transform="rotate(315 9 9)"/>'
    ),
    "activity": (
        '<path d="M2 9.5h3.2l1.8-5 2.6 9 1.8-6.5 1.4 2.5H16"/>'
    ),
    # A code-bracket mark for the OpenAPI/Swagger reference — see the
    # "External" section note below on why this lives in the sidebar
    # rather than the topnav.
    "api-docs": (
        '<path d="M6.5 4.5 2 9l4.5 4.5M11.5 4.5 16 9l-4.5 4.5"/>'
    ),
}

# (section label, [(nav key, href, label)]). The nav key is what a route
# passes as ``active``, so a page and its menu entry cannot drift apart.
#
# Two groups, not seven: "Core domains" is everything that is this
# framework's own record (dataset -> run -> model -> lineage), and
# "Orchestration" is everything that is another system's record reached
# read-only (an Airflow DAG run — see airflow_gateway.py's module
# docstring for why that stays a distinct identity space from a
# TrainingRun even though it sits one eyebrow apart here).
#
# "Experiments" used to be its own entry here, a separate page reading
# MLflow's runs list. It no longer is: for a run this framework started,
# that page showed close to the same thing /runs/{id} already does (its
# own "MLflow" panel embeds provenance, charts, artifacts, model info —
# see run_detail.html), so the two pages left a reader guessing which
# one to open for no real difference. /runs now folds both in — pick an
# experiment there to switch to MLflow's own ranked view of it,
# including runs this framework never started (mlflow_gateway.py answers
# that request; see runs.html / initRuns()). /experiments and
# /experiments/{id} still redirect here for old links/bookmarks.
_NAV: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Core domains",
        [
            ("dashboard", "/dashboard", "Dashboard"),
            ("datasets", "/datasets", "Datasets"),
            ("runs", "/runs", "Runs"),
            ("models", "/models", "Model registry"),
            ("scheduling", "/schedules", "Scheduling"),
            ("lineage", "/lineage", "Lineage"),
        ],
    ),
    (
        "Orchestration",
        [
            ("pipelines", "/pipelines", "Pipelines"),
        ],
    ),
    (
        "System",
        [
            ("activity", "/activity", "Activity"),
            ("settings", "/settings", "Settings"),
        ],
    ),
    (
        "External",
        [
            # Same origin (FastAPI serves this itself), so a plain
            # in-tab navigation — unlike the topnav's hosted-docs-site
            # and GitHub links, which are actually external and open in
            # a new tab. See _sidebar()'s docstring for why this entry
            # exists at all.
            ("api-docs", "/docs", "API reference"),
        ],
    ),
]


def mount_ui(
    app: FastAPI,
    templates_dir: Path | None = None,
) -> None:
    """Mount the Management UI on ``app`` at ``/``.

    Args:
        app: The FastAPI application.
        templates_dir: Optional override for the directory holding the
            page fragments. Defaults to ``src/mlops_framework/ui/templates``.
    """
    pages = templates_dir or (_PKG_UI / "templates")
    static_dir = _PKG_UI / "static"

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _page(pages, "dashboard.html", "Dashboard", "dashboard")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard() -> str:
        return _page(pages, "dashboard.html", "Dashboard", "dashboard")

    @app.get("/datasets", response_class=HTMLResponse)
    def datasets() -> str:
        return _page(pages, "datasets.html", "Datasets", "datasets")

    @app.get("/datasets/{dataset_id}", response_class=HTMLResponse)
    def dataset_detail(dataset_id: int) -> str:
        return _page(
            pages, "dataset_detail.html", f"Dataset #{dataset_id}", "datasets"
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs() -> str:
        return _page(pages, "runs.html", "Runs", "runs")

    # Registered before /runs/{run_id} so "compare" is not swallowed by the
    # int path converter (which would 422 on a non-numeric segment).
    #
    # active="runs", not a "compare" key of its own: this page is only
    # reached via the Compare button on /runs (see initRuns()'s
    # compareBtn), not its own sidebar entry, so the sidebar should keep
    # highlighting Runs rather than nothing.
    @app.get("/runs/compare", response_class=HTMLResponse)
    def runs_compare() -> str:
        return _page(pages, "runs_compare.html", "Compare runs", "runs")

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: int) -> str:
        return _page(pages, "run_detail.html", f"Run #{run_id}", "runs")

    @app.get("/models", response_class=HTMLResponse)
    def models() -> str:
        return _page(pages, "models.html", "Model registry", "models")

    @app.get("/models/{model_id}", response_class=HTMLResponse)
    def model_detail(model_id: int) -> str:
        return _page(pages, "model_detail.html", f"Model #{model_id}", "models")

    @app.get("/schedules", response_class=HTMLResponse)
    def schedules() -> str:
        return _page(pages, "schedules.html", "Scheduling", "scheduling")

    @app.get("/lineage", response_class=HTMLResponse)
    def lineage() -> str:
        return _page(pages, "lineage.html", "Lineage", "lineage")

    # /experiments and /experiments/{id} used to be their own pages; both
    # folded into /runs (pick an experiment there — see _NAV's comment
    # on why). These redirect rather than 404 so an old bookmark or a
    # link from outside the console still lands somewhere useful.
    @app.get("/experiments")
    def experiments() -> RedirectResponse:
        return RedirectResponse("/runs")

    # The id is a string, not an int: MLflow experiment ids are opaque and
    # are not the framework's own integer primary keys.
    @app.get("/experiments/{experiment_id}")
    def experiment_detail(experiment_id: str) -> RedirectResponse:
        from urllib.parse import quote

        return RedirectResponse(f"/runs?experiment={quote(experiment_id, safe='')}")

    # A run reachable only by its raw MLflow run id — no framework
    # TrainingRun row (a sweep's child run, a run this framework never
    # started). "runs" stays the active nav key: this is still a run
    # detail page, just fed by /mlflow/runs/{id} instead of
    # /training-runs/{id} — see run_detail.html's framework-run sibling.
    @app.get("/mlflow-runs/{mlflow_run_id}", response_class=HTMLResponse)
    def mlflow_run_detail(mlflow_run_id: str) -> str:
        return _page(
            pages,
            "mlflow_run_detail.html",
            f"MLflow run {mlflow_run_id[:12]}",
            "runs",
        )

    @app.get("/pipelines", response_class=HTMLResponse)
    def pipelines() -> str:
        return _page(pages, "pipelines.html", "Pipelines", "pipelines")

    # A dag_id is an Airflow-restricted string ([A-Za-z0-9_.-]), not this
    # framework's integer id scheme — same reasoning as experiment_id above.
    @app.get("/pipelines/{dag_id}", response_class=HTMLResponse)
    def pipeline_detail(dag_id: str) -> str:
        return _page(pages, "pipeline_detail.html", f"Pipeline {dag_id}", "pipelines")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page() -> str:
        return _page(pages, "settings.html", "Settings", "settings")

    @app.get("/activity", response_class=HTMLResponse)
    def activity() -> str:
        return _page(pages, "activity.html", "Activity", "activity")


# ---------------------------------------------------------------------- #
# Shell composition
# ---------------------------------------------------------------------- #


def _page(pages: Path, name: str, title: str, active: str) -> str:
    """Wrap a page fragment in the console shell."""
    target = pages / name
    fragment = (
        target.read_text(encoding="utf-8")
        if target.exists()
        else (
            f'<h1 class="page-title">{title}</h1>'
            '<p class="page-desc">Page not yet implemented.</p>'
        )
    )
    return _document(title, active, fragment)


def _icon(key: str) -> str:
    body = _ICONS.get(key, "")
    return (
        '<svg class="nav-icon" viewBox="0 0 18 18" fill="none" '
        'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def _nav_link(key: str, href: str, label: str, active: str) -> str:
    """Render one side-navigation entry.

    Split out of :func:`_sidebar` rather than inlined as an f-string: the
    markup needs escaped quotes, and an f-string expression may not
    contain a backslash before Python 3.12 (PEP 701). The package
    supports 3.11, so building the two conditional fragments as plain
    locals first is the portable form.
    """
    is_active = key == active
    css = "nav-item active" if is_active else "nav-item"
    current = ' aria-current="page"' if is_active else ""
    return (
        f'<a class="{css}" href="{href}"{current}>'
        f"{_icon(key)}<span>{label}</span></a>"
    )


def _sidebar(active: str) -> str:
    """Render the contextual left navigation for the active page.

    Ends with a trailing "External" section holding one entry — the
    Swagger/OpenAPI reference at ``/docs``. That link used to live in
    the topnav instead (no sidebar duplication needed), but the topnav
    slot next to the GitHub link now points at the hosted documentation
    site (see ``_document()``'s header markup), which left ``/docs``
    reachable from nowhere in the console — the same regression
    ``tests/api/test_ui.py::TestTopNav`` was written to catch the first
    time this link moved. This is the fix: one entry here instead of a
    second link in the topnav, so ``/docs`` has exactly one place it
    lives rather than an inconsistent second copy.
    """
    blocks: list[str] = []
    for section, items in _NAV:
        links = "".join(
            _nav_link(key, href, label, active) for key, href, label in items
        )
        blocks.append(
            f'<div class="nav-section">'
            f'<div class="nav-section-title">{section}</div>{links}</div>'
        )
    return "".join(blocks)


def _document(title: str, active: str, fragment: str) -> str:
    """Compose the full HTML document around a page fragment.

    ``app.js`` is loaded in ``<head>`` without ``defer`` on purpose: each
    fragment ends with an inline ``init*()`` call, and inline scripts run
    during parsing — ahead of any deferred script. Loading it eagerly is
    what keeps that call valid. The file only declares functions at the
    top level, so nothing runs before the DOM it needs exists.
    """
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} &middot; {BRAND}</title>
<link rel="icon" href="/static/favicon.svg?v={_FAVICON_VERSION}" type="image/svg+xml">
<link rel="alternate icon" href="/static/favicon.png?v={_FAVICON_VERSION}" sizes="32x32">
<link rel="stylesheet" href="/static/app.css?v={_CSS_VERSION}">
<link rel="preload" href="/static/fonts/Inter-Variable.woff2" as="font" type="font/woff2" crossorigin>
<script>
  // Applied before first paint so a dark-theme reload does not flash white.
  try {{
    var t = localStorage.getItem("gateflow-theme");
    if (t) document.documentElement.dataset.theme = t;
  }} catch (e) {{}}
</script>
<script src="/static/app.js?v={_JS_VERSION}"></script>
</head>
<body>
<a class="skip-link" href="#content">Skip to main content</a>

<header class="topnav">
  <button class="nav-toggle" id="nav-toggle" aria-label="Toggle navigation"
          aria-controls="sidenav" aria-expanded="true">
    <span class="bars" aria-hidden="true"><i></i><i></i><i></i></span>
  </button>
  <a class="brand" href="/dashboard" aria-label="{BRAND}">
    <svg class="brand-mark" viewBox="95 95 693 714" fill="none" aria-hidden="true">
      <path class="lm-accent"
            d="M708.8 204A357 357 0 1 0 525 801.5V688A247 247 0 1 1 629.7 280.4Z"/>
      <path class="lm-deep"
            d="M450 424H788V610A400 400 0 0 1 612 800V616L533 536L612 566V524H450Z"/>
      <path class="lm-deep" d="M573 638a7 7 0 0 1 7 7v20a7 7 0 0 1-14 0v-20a7 7 0 0 1 7-7Z"/>
    </svg>
    <span class="brand-name" aria-hidden="true"
      ><span class="bn-accent">ate</span><span class="bn-deep">flow</span></span>
  </a>
  <div class="topnav-spacer"></div>

  <nav class="topnav-util" aria-label="Utilities">
    <!-- The hosted docs site (docs-site/, mkdocs.yml), built and deployed
         by .github/workflows/docs.yml on every push to docs-site/. Used
         to point at this app's own Swagger UI (/docs) — the interactive
         API reference is still there and still linked from the REST API
         Reference page, but the framework's actual "how do I use this"
         guide belongs in the nav slot next to the repo link, not the
         other way around. -->
    <a class="topnav-btn" href="https://minhquang1604.github.io/Gateflow/"
       target="_blank" rel="noopener">Docs</a>
    <a class="topnav-btn" href="https://github.com/minhquang1604/Gateflow"
       target="_blank" rel="noopener" aria-label="View source on GitHub">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
        <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
      </svg>
    </a>
    <button id="theme-toggle" class="topnav-btn" aria-label="Toggle colour theme">
      <svg class="theme-icon theme-icon-sun" viewBox="0 0 18 18" fill="none"
           stroke="currentColor" stroke-width="1.4" stroke-linecap="round" aria-hidden="true">
        <circle cx="9" cy="9" r="2.6" fill="currentColor" stroke="none"/>
        <line x1="13.40" y1="9" x2="15.30" y2="9"/>
        <line x1="12.11" y1="12.11" x2="13.45" y2="13.45"/>
        <line x1="9" y1="13.40" x2="9" y2="15.30"/>
        <line x1="5.89" y1="12.11" x2="4.55" y2="13.45"/>
        <line x1="4.60" y1="9" x2="2.70" y2="9"/>
        <line x1="5.89" y1="5.89" x2="4.55" y2="4.55"/>
        <line x1="9" y1="4.60" x2="9" y2="2.70"/>
        <line x1="12.11" y1="5.89" x2="13.45" y2="4.55"/>
      </svg>
      <svg class="theme-icon theme-icon-moon" viewBox="0 0 18 18" fill="currentColor" aria-hidden="true">
        <path d="M15.75 9.59A6.75 6.75 0 1 1 8.41 2.25 5.25 5.25 0 0 0 15.75 9.59z"/>
      </svg>
    </button>
  </nav>
</header>

<div class="app-shell">
  <div class="nav-scrim" id="nav-scrim" hidden></div>
  <aside class="sidenav" id="sidenav" aria-label="Service navigation">
    {_sidebar(active)}
  </aside>
  <main id="content">
{fragment}
  </main>
</div>

<script>initShell();</script>
</body>
</html>
"""
