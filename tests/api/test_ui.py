"""End-to-end tests for the Management UI.

These tests boot the full FastAPI app (API + UI) and confirm:

* Every UI route returns 200 with a non-empty HTML body.
* The static assets (``/static/app.css``, ``/static/app.js``) are served.
* The JavaScript file is syntactically valid JavaScript (smoke check via
  Node or a structural check that every function name referenced in HTML
  templates is defined in the file).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mlops_framework.api.app import create_app


PAGES = [
    "/",
    "/dashboard",
    "/datasets",
    "/runs",
    "/models",
    "/lineage",
    "/datasets/1",  # even with no data, the page itself loads
    "/runs/1",
    "/models/1",
]


@pytest.fixture()
def ui_client():
    """Build an app with the UI mounted."""
    app = create_app(mount_ui=True)
    return TestClient(app)


class TestUIPages:
    @pytest.mark.parametrize("path", PAGES)
    def test_page_returns_200(self, ui_client, path):
        r = ui_client.get(path)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        body = r.text
        assert "<html" in body.lower()
        assert "</html>" in body.lower()


class TestTopNav:
    """The OpenAPI docs must stay reachable from the console.

    ``mount.py``'s ``_sidebar`` docstring gives "the topnav's own API
    link already covers it" as the reason there is no API entry in the
    sidebar. That link was once replaced by a GitHub icon, which left
    /docs unreachable from anywhere in the console while the docstring
    (and the README's Gateflow section) still said otherwise. This
    holds the two halves together.
    """

    def test_docs_link_is_present(self, ui_client):
        body = ui_client.get("/dashboard").text
        assert 'href="/docs"' in body

    def test_docs_actually_serves_openapi(self, ui_client):
        assert ui_client.get("/docs").status_code == 200
        assert ui_client.get("/openapi.json").status_code == 200

    def test_sidebar_still_has_no_api_entry(self, ui_client):
        """The other half of the same reasoning: one link, not two."""
        body = ui_client.get("/dashboard").text
        sidebar = body.split('class="sidenav"', 1)[1].split("</aside>", 1)[0]
        assert "/docs" not in sidebar


class TestStaticAssets:
    def test_css_served(self, ui_client):
        r = ui_client.get("/static/app.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]
        assert "--bg" in r.text  # our CSS custom property

    def test_js_served(self, ui_client):
        r = ui_client.get("/static/app.js")
        assert r.status_code == 200
        assert "javascript" in r.headers["content-type"]
        assert "async function api" in r.text


class TestJSApiSurface:
    """Static check: every ``init*`` function referenced from a template
    is defined in ``app.js``. Catches typos at test time."""

    def test_init_functions_defined(self):
        tdir = Path(__file__).parent.parent.parent / "src/mlops_framework/ui/templates"
        js = (tdir.parent / "static" / "app.js").read_text(encoding="utf-8")

        referenced = set()
        for html in tdir.glob("*.html"):
            for m in re.finditer(r"init([A-Z][A-Za-z]+)\s*\(", html.read_text()):
                referenced.add("init" + m.group(1))

        defined = set(re.findall(r"async function init([A-Z][A-Za-z]+)", js))
        defined = {"init" + name for name in defined}
        missing = referenced - defined
        assert not missing, f"Template references unknown init functions: {missing}"


class TestUIIntegration:
    def test_dashboard_loads_with_seeded_data(self, ui_client, session_factory):
        """End-to-end: seed data via the DB, confirm the page HTML is still
        served correctly (JS will populate the KPI grid in the browser).
        """
        from mlops_framework.database.models.dataset import Dataset
        s = session_factory()
        try:
            s.add(Dataset(name="test-ds", description="x"))
            s.commit()
        finally:
            s.close()

        r = ui_client.get("/dashboard")
        assert r.status_code == 200
        # The HTML contains the JS bootstrap call
        assert "initDashboard" in r.text
