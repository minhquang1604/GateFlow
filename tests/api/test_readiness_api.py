"""Tests for /api/readiness."""

from __future__ import annotations

import json


def _seed(session_factory, with_eval=True):
    from mlops_framework.database.models.dataset import Dataset
    from mlops_framework.database.models.dataset_version import DatasetVersion
    from mlops_framework.database.models.readiness_evaluation import (
        ReadinessEvaluation,
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
        if with_eval:
            s.add(
                ReadinessEvaluation(
                    dataset_version_id=dv.id,
                    status="READY",
                    checks_json=json.dumps({"size": {"passed": True}}),
                    reasons_json=json.dumps([]),
                    policy_json=json.dumps({"required_size": 5}),
                )
            )
        s.commit()
    finally:
        s.close()


class TestReadiness:
    def test_no_eval_returns_null(self, client, session_factory):
        _seed(session_factory, with_eval=False)
        r = client.get("/api/readiness/1")
        assert r.status_code == 200
        assert r.json() is None

    def test_returns_latest_evaluation(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/readiness/1")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "READY"
        assert body["checks"] == {"size": {"passed": True}}
        assert body["policy"] == {"required_size": 5}
        assert body["reasons"] == []

    def test_404_for_unknown_version(self, client):
        r = client.get("/api/readiness/9999")
        assert r.status_code == 404
