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
        return ds.id, dv.id, run.id, mv.id
    finally:
        s.close()


class TestLineage:
    def test_dataset(self, client, session_factory):
        dataset_id, *_ = _seed(session_factory)
        r = client.get(f"/api/lineage/dataset/{dataset_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["root_kind"] == "DatasetVersion"
        types = {n["type"] for n in body["nodes"]}
        assert {"DatasetVersion", "TrainingRun", "ModelVersion"} <= types
        # No separate Dataset/Model identity nodes — folded into the
        # version node's own label. See LineageManager's module
        # docstring.
        assert "Dataset" not in types
        assert "Model" not in types

    def test_dataset_version(self, client, session_factory):
        _, dv_id, *_ = _seed(session_factory)
        r = client.get(f"/api/lineage/dataset-version/{dv_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["root_kind"] == "DatasetVersion"
        types = {n["type"] for n in body["nodes"]}
        assert "DatasetVersion" in types
        assert "TrainingRun" in types
        assert "Dataset" not in types

    def test_model_version(self, client, session_factory):
        _, _, _, mv_id = _seed(session_factory)
        r = client.get(f"/api/lineage/model-version/{mv_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["root_kind"] == "ModelVersion"
        types = {n["type"] for n in body["nodes"]}
        assert {"ModelVersion", "TrainingRun", "DatasetVersion"} <= types
        assert "Model" not in types

    def test_training_run(self, client, session_factory):
        _, _, run_id, _ = _seed(session_factory)
        r = client.get(f"/api/lineage/training-run/{run_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["root_kind"] == "TrainingRun"

    def test_no_has_version_or_trained_on_edges_anywhere(
        self, client, session_factory
    ):
        _, dv_id, *_ = _seed(session_factory)
        body = client.get(f"/api/lineage/dataset-version/{dv_id}").json()
        edge_types = {e["type"] for e in body["edges"]}
        assert "has_version" not in edge_types
        assert "trained_on" not in edge_types

    def test_labels_carry_the_name(self, client, session_factory):
        dataset_id, dv_id, _, mv_id = _seed(session_factory)
        body = client.get(f"/api/lineage/dataset/{dataset_id}").json()
        dv_node = next(n for n in body["nodes"] if n["id"] == f"DatasetVersion:{dv_id}")
        mv_node = next(n for n in body["nodes"] if n["id"] == f"ModelVersion:{mv_id}")
        assert dv_node["label"] == "d1 v1"
        assert mv_node["label"] == "m1 v1"

    def test_404(self, client):
        for path in (
            "/api/lineage/dataset/9999",
            "/api/lineage/dataset-version/9999",
            "/api/lineage/model-version/9999",
            "/api/lineage/training-run/9999",
        ):
            assert client.get(path).status_code == 404
