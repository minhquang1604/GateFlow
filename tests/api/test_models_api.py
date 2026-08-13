"""Tests for /api/models and /api/model-versions."""

from __future__ import annotations

import json


def _seed(session_factory):
    from mlops_framework.database.models.dataset import Dataset
    from mlops_framework.database.models.dataset_version import DatasetVersion
    from mlops_framework.database.models.model import Model as ModelRow
    from mlops_framework.database.models.model_version import (
        ModelState,
        ModelVersion,
    )
    from mlops_framework.database.models.training_run import (
        RunStatus,
        TrainingRun,
    )

    s = session_factory()
    try:
        ds = Dataset(name="d1")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id,
            version_number=1,
            storage_uri="s3://x",
            checksum="a" * 64,
            schema_hash="b" * 64,
            row_count=10,
        )
        s.add(dv)
        s.flush()
        run = TrainingRun(
            dataset_version_id=dv.id, status=RunStatus.SUCCESS.value
        )
        s.add(run)
        s.flush()
        m = ModelRow(name="m1", task="classification")
        s.add(m)
        s.flush()
        s.add(
            ModelVersion(
                model_id=m.id,
                version_number=1,
                state=ModelState.CANDIDATE.value,
                dataset_version_id=dv.id,
                training_run_id=run.id,
                metrics_json=json.dumps({"f1": 0.7}),
            )
        )
        s.add(
            ModelVersion(
                model_id=m.id,
                version_number=2,
                state=ModelState.PRODUCTION.value,
                dataset_version_id=dv.id,
                training_run_id=run.id,
                metrics_json=json.dumps({"f1": 0.9}),
            )
        )
        s.commit()
    finally:
        s.close()


class TestModels:
    def test_empty(self, client):
        r = client.get("/api/models")
        assert r.status_code == 200
        assert r.json() == []

    def test_list(self, client, session_factory):
        _seed(session_factory)
        body = client.get("/api/models").json()
        assert len(body) == 1
        m = body[0]
        assert m["name"] == "m1"
        assert m["task"] == "classification"
        assert m["version_count"] == 2
        # The PRODUCTION version is exposed
        assert m["production_version"]["version_number"] == 2
        assert m["production_version"]["state"] == "PRODUCTION"
        assert m["production_version"]["metrics"] == {"f1": 0.9}

    def test_get_model(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/models/1")
        assert r.status_code == 200
        assert r.json()["name"] == "m1"

    def test_get_model_404(self, client):
        r = client.get("/api/models/9999")
        assert r.status_code == 404

    def test_list_versions(self, client, session_factory):
        _seed(session_factory)
        body = client.get("/api/models/1/versions").json()
        assert [v["version_number"] for v in body] == [1, 2]
        # v1 is CANDIDATE, v2 is PRODUCTION
        assert body[0]["state"] == "CANDIDATE"
        assert body[1]["state"] == "PRODUCTION"
        # Source dataset and source run are exposed
        assert body[1]["dataset_version_id"] == 1
        assert body[1]["training_run_id"] == 1

    def test_list_versions_404(self, client):
        r = client.get("/api/models/9999/versions")
        assert r.status_code == 404

    def test_get_model_version(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/model-versions/2")
        assert r.status_code == 200
        body = r.json()
        assert body["version_number"] == 2
        assert body["state"] == "PRODUCTION"

    def test_get_model_version_404(self, client):
        r = client.get("/api/model-versions/9999")
        assert r.status_code == 404


class TestModelVersionReport:
    """The HTTP side of sdk/report.py — content assembly itself is
    covered by tests/sdk/test_report.py; these pin the wire behaviour
    (media type, download headers, error mapping)."""

    def test_markdown_is_served_as_a_download(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/model-versions/2/report")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/markdown")
        assert "attachment" in r.headers["content-disposition"]
        assert "model-version-2-report.md" in r.headers["content-disposition"]
        assert "# Reproducibility report — m1 v2" in r.text
        # The dataset content hash is what makes the report reproducible.
        assert "a" * 64 in r.text

    def test_html_format(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/model-versions/2/report", params={"format": "html"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "model-version-2-report.html" in r.headers["content-disposition"]
        assert r.text.startswith("<!doctype html>")

    def test_unknown_version_is_404(self, client):
        assert client.get("/api/model-versions/9999/report").status_code == 404

    def test_bad_format_is_422(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/model-versions/2/report", params={"format": "pdf"})
        assert r.status_code == 422
