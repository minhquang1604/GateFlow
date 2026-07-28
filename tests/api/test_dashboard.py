"""Tests for /api/dashboard."""

from __future__ import annotations


class TestDashboard:
    def test_empty_returns_zero(self, client):
        r = client.get("/api/dashboard")
        assert r.status_code == 200
        body = r.json()
        assert body["datasets"] == 0
        assert body["dataset_versions"] == 0
        assert body["total_runs"] == 0
        assert body["active_runs"] == 0
        assert body["success_runs"] == 0
        assert body["failed_runs"] == 0
        assert body["models"] == 0
        assert body["production_models"] == 0
        assert body["success_rate"] == 0.0

    def test_counts_reflect_seeded_data(self, client, session_factory):
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
            ds = Dataset(name="d1", description="d")
            s.add(ds)
            s.flush()
            s.add(
                DatasetVersion(
                    dataset_id=ds.id,
                    version_number=1,
                    storage_uri="s3://x",
                    checksum="a" * 64,
                    schema_hash="b" * 64,
                    row_count=100,
                )
            )
            m = ModelRow(name="m1", task="t")
            s.add(m)
            s.flush()
            mv = ModelVersion(
                model_id=m.id,
                version_number=1,
                state=ModelState.PRODUCTION.value,
                dataset_version_id=1,
            )
            s.add(mv)
            s.add(
                TrainingRun(
                    dataset_version_id=1,
                    status=RunStatus.SUCCESS.value,
                )
            )
            s.add(
                TrainingRun(
                    dataset_version_id=1,
                    status=RunStatus.FAILED.value,
                )
            )
            s.add(
                TrainingRun(
                    dataset_version_id=1,
                    status=RunStatus.RUNNING.value,
                )
            )
            s.commit()
        finally:
            s.close()

        r = client.get("/api/dashboard")
        body = r.json()
        assert body["datasets"] == 1
        assert body["dataset_versions"] == 1
        assert body["total_runs"] == 3
        assert body["active_runs"] == 1
        assert body["success_runs"] == 1
        assert body["failed_runs"] == 1
        assert body["models"] == 1
        assert body["production_models"] == 1
        # 1 success / (1 success + 1 failed) = 0.5
        assert body["success_rate"] == 0.5
