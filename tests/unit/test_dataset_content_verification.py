"""The training pipeline must refuse data that is not what was registered.

Lineage records intent — "this run was launched against dataset version
N". Nothing recorded what the worker actually read, so a file replaced at
its storage URI after registration produced a run that looked, in every
record the framework keeps, identical to a correct one.

These tests pin the check that closes that. They are deliberately about
the *negative* case: a verification that never fails is worthless, so the
mismatch path is what has to stay proven.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from case_studies.fraud_detection.pipelines import _source_sha256, train_xgboost

pytest.importorskip("pandas")
pytest.importorskip("xgboost")


HEADER = "time,amount," + ",".join(f"v{i}" for i in range(1, 29)) + ",class\n"


def _write_csv(path, rows=200):
    body = "".join(
        f"{i},{i * 1.5}," + ",".join(str((i + j) % 7 * 0.1) for j in range(28))
        + f",{i % 2}\n"
        for i in range(rows)
    )
    path.write_text(HEADER + body, encoding="utf-8")
    return path


@pytest.fixture()
def csv_file(tmp_path):
    return _write_csv(tmp_path / "data.csv")


class TestSourceHash:
    def test_matches_hashlib(self, csv_file):
        expected = hashlib.sha256(csv_file.read_bytes()).hexdigest()
        assert _source_sha256(str(csv_file)) == expected

    def test_chunked_read_matches_whole_file(self, tmp_path):
        """The hash streams in 1 MB chunks; a file over that boundary must
        still hash the same as reading it in one go."""
        big = _write_csv(tmp_path / "big.csv", rows=40_000)
        assert big.stat().st_size > 1024 * 1024
        assert _source_sha256(str(big)) == hashlib.sha256(big.read_bytes()).hexdigest()

    def test_unreadable_source_returns_none(self, tmp_path):
        assert _source_sha256(str(tmp_path / "missing.csv")) is None

    def test_file_scheme_is_stripped(self, csv_file):
        assert _source_sha256(f"file://{csv_file}") == _source_sha256(str(csv_file))


class TestTrainingRefusesChangedData:
    def test_matching_digest_trains(self, csv_file):
        result = train_xgboost(
            {
                "csv_uri": str(csv_file),
                "dataset_content_sha256": _source_sha256(str(csv_file)),
                "n_estimators": 5,
            }
        )
        assert result["status"] == "SUCCESS"

    def test_changed_file_fails_the_run(self, csv_file):
        """The point of the whole exercise."""
        registered = _source_sha256(str(csv_file))
        with csv_file.open("a", encoding="utf-8") as fh:
            fh.write("999,1.0," + ",".join("0.5" for _ in range(28)) + ",1\n")

        result = train_xgboost(
            {
                "csv_uri": str(csv_file),
                "dataset_content_sha256": registered,
                "n_estimators": 5,
            }
        )
        assert result["status"] == "FAILED"
        assert "does not match the registered version" in result["error"]
        assert registered in result["error"]

    def test_absent_digest_still_trains(self, csv_file):
        """Versions registered without a content hash cannot be checked;
        that must not turn into a failed run."""
        result = train_xgboost({"csv_uri": str(csv_file), "n_estimators": 5})
        assert result["status"] == "SUCCESS"

    def test_unreadable_source_returns_status_not_traceback(self, tmp_path):
        """The pipeline's contract is to always hand back a status dict —
        a missing file used to escape as an uncaught FileNotFoundError."""
        result = train_xgboost({"csv_uri": str(tmp_path / "gone.csv")})
        assert result["status"] == "FAILED"
        assert "gone.csv" in result["error"]


class TestConfigCarriesTheDigest:
    """The pipeline can only check what the framework forwards to it."""

    @staticmethod
    def _service_with_version(metadata):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from mlops_framework.database.base import Base
        from mlops_framework.database.models.dataset import Dataset
        from mlops_framework.database.models.dataset_version import DatasetVersion
        from mlops_framework.dataset.manager import DatasetManager
        from mlops_framework.training.manager import TrainingManager
        from mlops_framework.training.service import TrainingService
        from tests.unit.test_training_service_config import _RecordingOrchestrator

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        ds = Dataset(name="credit-card-fraud")
        session.add(ds)
        session.flush()
        session.add(
            DatasetVersion(
                dataset_id=ds.id,
                version_number=1,
                storage_uri="s3://bucket/creditcard.csv",
                checksum="a" * 64,
                schema_hash="b" * 64,
                row_count=10,
                metadata_json=json.dumps(metadata) if metadata is not None else None,
            )
        )
        session.commit()

        orch = _RecordingOrchestrator()
        dm = DatasetManager(session)
        service = TrainingService(
            training_manager=TrainingManager(session, dm),
            orchestrator=orch,
            tracker=None,
        )
        return service, orch

    def test_forwards_content_sha256_from_version_metadata(self):
        digest = "c" * 64
        service, orch = self._service_with_version({"content_sha256": digest})
        run = service.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        service.start_run(run.id)

        _, config = orch.triggered[0]
        assert config["dataset_content_sha256"] == digest

    def test_omits_the_key_when_no_digest_was_registered(self):
        """Absent, not empty: the pipeline treats any value as something to
        check against, so a blank one would fail every run."""
        service, orch = self._service_with_version({"columns": []})
        run = service.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        service.start_run(run.id)

        _, config = orch.triggered[0]
        assert "dataset_content_sha256" not in config

    def test_survives_unparseable_metadata(self):
        service, orch = self._service_with_version(None)
        run = service.create_run(dataset_version_id=1, pipeline_id="pkg.mod:train")
        service.start_run(run.id)

        _, config = orch.triggered[0]
        assert "dataset_content_sha256" not in config

    def test_version_checksum_is_not_a_content_hash(self):
        """DatasetVersion.checksum hashes the storage URI plus the metadata
        dict, never the file, so forwarding it instead would have produced a
        check that can never fail. Pinned because the two fields sit next to
        each other and the wrong one is the tempting one."""
        from mlops_framework.dataset.manager import DatasetManager

        engine_free = DatasetManager.__new__(DatasetManager)
        same_uri_different_bytes = engine_free._calculate_version_checksum(
            "s3://bucket/creditcard.csv", {"row_count": 10}
        )
        again = engine_free._calculate_version_checksum(
            "s3://bucket/creditcard.csv", {"row_count": 10}
        )
        assert same_uri_different_bytes == again  # content plays no part
