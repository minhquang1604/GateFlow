"""Tests for /api/training-runs."""

from __future__ import annotations

import json


def _seed(session_factory, runs):
    from mlops_framework.database.models.dataset import Dataset
    from mlops_framework.database.models.dataset_version import DatasetVersion
    from mlops_framework.database.models.training_run import TrainingRun

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
        for r in runs:
            meta = {}
            if r.get("parameters") is not None:
                meta["parameters"] = r["parameters"]
            if r.get("metrics") is not None:
                meta["metrics"] = r["metrics"]
            s.add(
                TrainingRun(
                    dataset_version_id=dv.id,
                    status=r["status"],
                    metadata_json=json.dumps(meta) if meta else None,
                    error_message=r.get("error_message"),
                )
            )
        s.commit()
    finally:
        s.close()


class TestRunsList:
    def test_empty(self, client):
        r = client.get("/api/training-runs")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_with_status_filter(self, client, session_factory):
        _seed(
            session_factory,
            [
                {"status": "SUCCESS", "parameters": {"lr": 0.1}, "metrics": {"f1": 0.9}},
                {"status": "FAILED", "error_message": "boom"},
                {"status": "RUNNING"},
            ],
        )
        r = client.get("/api/training-runs")
        body = r.json()
        assert len(body) == 3

        r2 = client.get("/api/training-runs?status=SUCCESS")
        body2 = r2.json()
        assert len(body2) == 1
        assert body2[0]["status"] == "SUCCESS"
        assert body2[0]["parameters"] == {"lr": 0.1}
        assert body2[0]["metrics"] == {"f1": 0.9}

    def test_list_newest_first(self, client, session_factory):
        _seed(
            session_factory,
            [
                {"status": "SUCCESS"},
                {"status": "FAILED"},
                {"status": "RUNNING"},
            ],
        )
        body = client.get("/api/training-runs").json()
        # Newest id first
        assert body[0]["id"] > body[1]["id"] > body[2]["id"]

    def test_get_run(self, client, session_factory):
        _seed(
            session_factory,
            [{"status": "SUCCESS", "parameters": {"x": 1}, "metrics": {"a": 2}}],
        )
        r = client.get("/api/training-runs/1")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "SUCCESS"
        assert body["parameters"] == {"x": 1}
        assert body["metrics"] == {"a": 2}

    def test_get_run_404(self, client):
        r = client.get("/api/training-runs/9999")
        assert r.status_code == 404
