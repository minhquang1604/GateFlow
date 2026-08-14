"""Tests for /api/datasets and /api/dataset-versions."""

from __future__ import annotations


def _make_dataset(client, name="d1"):

    # The client uses the same DB; we re-use the same engine through
    # the session factory fixture.
    return None  # placeholder to keep imports clean


class TestDatasetsList:
    def test_empty(self, client):
        r = client.get("/api/datasets")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_with_versions(self, client, session_factory):
        from mlops_framework.dataset.manager import DatasetManager

        s = session_factory()
        try:
            dm = DatasetManager(s)
            ds = dm.create_dataset(name="d1", description="desc")
            dm.create_version(
                dataset_id=ds.id,
                storage_uri="s3://b/v1.csv",
                row_count=100,
                metadata={"columns": [{"name": "x", "dtype": "int64"}]},
            )
            dm.create_version(
                dataset_id=ds.id,
                storage_uri="s3://b/v2.csv",
                row_count=200,
            )
            s.commit()
        finally:
            s.close()

        r = client.get("/api/datasets")
        body = r.json()
        assert len(body) == 1
        ds = body[0]
        assert ds["name"] == "d1"
        assert ds["version_count"] == 2
        assert ds["latest_version"]["version_number"] == 2
        assert ds["latest_version"]["row_count"] == 200
        assert ds["latest_version"]["metadata"] is None  # No columns in v2

    def test_get_dataset_404(self, client):
        r = client.get("/api/datasets/9999")
        assert r.status_code == 404

    def test_get_dataset(self, client, session_factory):
        from mlops_framework.dataset.manager import DatasetManager

        s = session_factory()
        try:
            dm = DatasetManager(s)
            ds = dm.create_dataset(name="d1", description="desc")
            dm.create_version(
                dataset_id=ds.id,
                storage_uri="s3://b/v1.csv",
                row_count=100,
            )
            s.commit()
        finally:
            s.close()

        r = client.get("/api/datasets/1")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "d1"
        assert body["version_count"] == 1
        assert body["latest_version"]["row_count"] == 100

    def test_list_versions(self, client, session_factory):
        from mlops_framework.dataset.manager import DatasetManager

        s = session_factory()
        try:
            dm = DatasetManager(s)
            ds = dm.create_dataset(name="d1")
            for i in range(1, 4):
                dm.create_version(
                    dataset_id=ds.id,
                    storage_uri=f"s3://b/v{i}.csv",
                    row_count=i * 100,
                    metadata={"columns": [{"name": "x", "dtype": "int64"}]},
                )
            s.commit()
        finally:
            s.close()

        r = client.get("/api/datasets/1/versions")
        assert r.status_code == 200
        body = r.json()
        assert [v["version_number"] for v in body] == [1, 2, 3]
        # Metadata is preserved
        assert all(v["metadata"] is not None for v in body)
        # Checksum is a 64-hex string
        assert all(len(v["checksum"]) == 64 for v in body)

    def test_get_dataset_version(self, client, session_factory):
        from mlops_framework.dataset.manager import DatasetManager

        s = session_factory()
        try:
            dm = DatasetManager(s)
            ds = dm.create_dataset(name="d1")
            dm.create_version(
                dataset_id=ds.id,
                storage_uri="s3://b/v1.csv",
                row_count=42,
            )
            s.commit()
        finally:
            s.close()

        r = client.get("/api/dataset-versions/1")
        assert r.status_code == 200
        body = r.json()
        assert body["row_count"] == 42
        assert body["storage_uri"] == "s3://b/v1.csv"

    def test_get_dataset_version_404(self, client):
        r = client.get("/api/dataset-versions/9999")
        assert r.status_code == 404
