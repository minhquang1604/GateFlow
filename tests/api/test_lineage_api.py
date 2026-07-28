"""Tests for /api/lineage."""

from __future__ import annotations


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
        m = ModelRow(name="m1")
        s.add(m)
        s.flush()
        mv = ModelVersion(
            model_id=m.id,
            version_number=1,
            state=ModelState.PRODUCTION.value,
            dataset_version_id=dv.id,
            training_run_id=run.id,
        )
        s.add(mv)
        s.commit()
    finally:
        s.close()


class TestLineage:
    def test_dataset_version(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/lineage/dataset-version/1")
        assert r.status_code == 200
        body = r.json()
        assert body["root_kind"] == "DatasetVersion"
        types = {n["type"] for n in body["nodes"]}
        assert "Dataset" in types
        assert "DatasetVersion" in types
        assert "TrainingRun" in types

    def test_model_version(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/lineage/model-version/1")
        assert r.status_code == 200
        body = r.json()
        assert body["root_kind"] == "ModelVersion"
        types = {n["type"] for n in body["nodes"]}
        assert {"Model", "ModelVersion", "TrainingRun", "DatasetVersion"} <= types

    def test_training_run(self, client, session_factory):
        _seed(session_factory)
        r = client.get("/api/lineage/training-run/1")
        assert r.status_code == 200
        body = r.json()
        assert body["root_kind"] == "TrainingRun"

    def test_404(self, client):
        for path in (
            "/api/lineage/dataset-version/9999",
            "/api/lineage/model-version/9999",
            "/api/lineage/training-run/9999",
        ):
            assert client.get(path).status_code == 404
