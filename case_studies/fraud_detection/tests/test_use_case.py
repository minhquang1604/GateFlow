"""Tests for the Fraud Detection case study.

These tests verify that:

1. The synthetic data generator produces the expected shape.
2. The full SDK-driven lifecycle works end-to-end.
3. The case study's app code does not directly import any manager or
   internal module — it only uses the public SDK.
"""

from __future__ import annotations

import ast
import csv
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.session import DatabaseManager
from mlops_framework.orchestration.local import LocalDockerOrchestrator
from mlops_framework.sdk import (
    AlreadyExistsError,
    MLOpsProject,
    NotFoundError,
)
from mlops_framework.tracking.in_memory import InMemoryTracker

from case_studies.fraud_detection import data
from case_studies.fraud_detection.pipelines import (
    fail,
    train_advanced,
    train_baseline,
)


# ---------------------------------------------------------------------- #
# Data generator
# ---------------------------------------------------------------------- #


class TestDataGenerator:
    def test_generate_row_count(self):
        rows = list(data.generate(n_rows=100))
        assert len(rows) == 100

    def test_columns_present(self):
        rows = list(data.generate(n_rows=10))
        expected = {"time", "amount", "class"} | {f"v{i}" for i in range(1, 29)}
        assert set(rows[0].keys()) == expected

    def test_class_distribution(self):
        rows = list(data.generate(n_rows=10000, fraud_ratio=0.01, seed=1))
        n_fraud = sum(1 for r in rows if r["class"] == 1)
        assert 80 <= n_fraud <= 120  # ~1% with seed 1

    def test_write_csv_roundtrip(self, tmp_path):
        p = data.write_csv(tmp_path / "fraud.csv", n_rows=50)
        with p.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 50
        assert "class" in rows[0]

    def test_schema_metadata_has_all_columns(self):
        meta = data.schema_metadata()
        cols = {c["name"] for c in meta["columns"]}
        assert "time" in cols
        assert "amount" in cols
        assert "class" in cols
        # 2 (time, amount) + 28 (v1..v28) + 1 (class) = 31
        assert len(meta["columns"]) == 31


# ---------------------------------------------------------------------- #
# Pipelines (called by orchestrator, not the SDK)
# ---------------------------------------------------------------------- #


class TestPipelines:
    def test_train_baseline_returns_metrics(self):
        out = train_baseline({"training_run_id": 1})
        assert out["status"] == "SUCCESS"
        assert "f1" in out["metrics"]
        assert "roc_auc" in out["metrics"]
        assert Path(out["artifact_path"]).exists()

    def test_train_advanced_returns_more_metrics(self):
        out = train_advanced({"training_run_id": 1})
        assert out["status"] == "SUCCESS"
        for k in ("f1", "precision", "recall"):
            assert k in out["metrics"]

    def test_fail_raises(self):
        with pytest.raises(RuntimeError):
            fail({})


# ---------------------------------------------------------------------- #
# SDK-driven lifecycle
# ---------------------------------------------------------------------- #


@pytest.fixture()
def project():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    mgr = DatabaseManager()
    mgr._engine = engine  # type: ignore[attr-defined]
    mgr._session_factory = factory  # type: ignore[attr-defined]

    orch = LocalDockerOrchestrator()
    p = MLOpsProject(
        "fraud",
        db_manager=mgr,
        orchestrator=orch,
        tracker=InMemoryTracker(),
    )
    p.register_pipeline("baseline", "case_studies.fraud_detection.pipelines:train_baseline")
    p.register_pipeline("advanced", "case_studies.fraud_detection.pipelines:train_advanced")
    p.register_pipeline("fail", "case_studies.fraud_detection.pipelines:fail")
    try:
        yield p
    finally:
        orch.shutdown()
        Base.metadata.drop_all(engine)
        engine.dispose()


class TestCaseStudyLifecycle:
    def test_full_lifecycle(self, project, tmp_path):
        csv_path = data.write_csv(tmp_path / "fraud.csv", n_rows=2000)
        ds = project.create_dataset("cc-fraud", description="credit card")
        v = ds.create_version(
            storage_uri=str(csv_path),
            row_count=2000,
            metadata=data.schema_metadata(),
        )
        m = project.create_model("fraud-xgb", task="binary_classification")

        run = project.train(
            dataset_version=v,
            pipeline="baseline",
            wait=True,
            timeout=30,
        )
        assert run.status == "SUCCESS"

        # Run a second pipeline on the same dataset
        run2 = project.train(
            dataset_version=v,
            pipeline="advanced",
            wait=True,
            timeout=30,
        )
        assert run2.status == "SUCCESS"

        # Lineage graph contains the dataset and both runs
        g = project.lineage.for_dataset_version(v.id)
        types = {n["type"] for n in g["nodes"]}
        assert "Dataset" in types
        assert "DatasetVersion" in types
        assert "TrainingRun" in types

    def test_duplicate_dataset_raises(self, project):
        project.create_dataset("dup")
        with pytest.raises(AlreadyExistsError):
            project.create_dataset("dup")

    def test_unknown_dataset_raises(self, project):
        with pytest.raises(NotFoundError):
            project.get_dataset("nope")

    def test_failing_pipeline_raises(self, project, tmp_path):
        csv_path = data.write_csv(tmp_path / "fraud.csv", n_rows=100)
        ds = project.create_dataset("ds")
        v = ds.create_version(str(csv_path), 100, metadata=data.schema_metadata())
        from mlops_framework.sdk.exceptions import TrainingError
        with pytest.raises(TrainingError):
            project.train(
                dataset_version=v, pipeline="fail", wait=True, timeout=10
            )


# ---------------------------------------------------------------------- #
# Static check: the case study imports only from the public SDK.
# ---------------------------------------------------------------------- #


class TestNoDirectManagerImports:
    """The case study's app code must use only the public SDK."""

    FORBIDDEN = (
        "mlops_framework.dataset.manager",
        "mlops_framework.training.manager",
        "mlops_framework.training.service",
        "mlops_framework.model.manager",
        "mlops_framework.orchestration",
        "mlops_framework.tracking",
        "mlops_framework.database",
        "mlops_framework.governance",
        "mlops_framework.readiness",
        "mlops_framework.drift",
        "mlops_framework.workflow",
        "mlops_framework.lineage",
        "mlops_framework.events",
    )

    def _imports(self, file: Path) -> set[str]:
        tree = ast.parse(file.read_text())
        return {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            node.names[0].name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        }

    def test_app_uses_only_sdk(self):
        here = Path(__file__).parent.parent
        app = here / "app.py"
        imports = self._imports(app)
        from_sdk = {"mlops_framework.sdk", "case_studies.fraud_detection"}
        # All other top-level imports from mlops_framework.* are forbidden
        leaked = {m for m in imports if m and m.startswith("mlops_framework.")} - from_sdk
        assert not leaked, f"app.py imports non-SDK internals: {leaked}"

    def test_pipelines_dont_import_framework(self):
        # Pipelines are called by the orchestrator inside a subprocess and
        # must not depend on the framework. They are app code.
        here = Path(__file__).parent.parent
        p = here / "pipelines.py"
        imports = self._imports(p)
        framework_imports = {m for m in imports if m and m.startswith("mlops_framework")}
        assert not framework_imports, f"pipelines.py imports framework: {framework_imports}"


# ---------------------------------------------------------------------- #
# Real-dataset column contract
# ---------------------------------------------------------------------- #


class TestColumnNormalisation:
    """Both data sources must reduce to one canonical column contract.

    The real Kaggle file is ``Time,V1..V28,Amount,Class``; the synthetic
    generator writes ``time,amount,v1..v28,class``. If these diverge the
    schema hash changes with the source, and the readiness engine starts
    rejecting a dataset for the wrong reason.
    """

    def _kaggle_frame(self):
        pd = pytest.importorskip("pandas")
        cols = ["Time"] + [f"V{i}" for i in range(1, 29)] + ["Amount", "Class"]
        return pd.DataFrame([[0.0] * 30 + [0]], columns=cols)

    def test_kaggle_header_maps_to_canonical(self):
        df = data.normalize_columns(self._kaggle_frame())
        assert list(df.columns) == data.CANONICAL_COLUMNS

    def test_synthetic_header_is_already_canonical(self, tmp_path):
        pd = pytest.importorskip("pandas")
        p = data.write_csv(tmp_path / "f.csv", n_rows=10)
        df = data.normalize_columns(pd.read_csv(p))
        assert list(df.columns) == data.CANONICAL_COLUMNS

    def test_both_sources_hash_to_the_same_schema(self, tmp_path):
        from mlops_framework.dataset.versioning import calculate_schema_hash

        kaggle = data.normalize_columns(self._kaggle_frame())
        synthetic = data.to_dataframe(data.write_csv(tmp_path / "f.csv", n_rows=10))
        spec = lambda df: [  # noqa: E731
            {"name": str(n), "dtype": "float64"} for n in df.columns
        ]
        assert calculate_schema_hash(spec(kaggle)) == calculate_schema_hash(spec(synthetic))

    def test_missing_column_raises_rather_than_misaligning(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame([[1.0, 2.0]], columns=["Time", "Amount"])
        with pytest.raises(ValueError, match="missing expected fraud columns"):
            data.normalize_columns(df)


class TestDescribeCsv:
    def test_profiles_a_csv_for_registration(self, tmp_path):
        pytest.importorskip("pandas")
        p = data.write_csv(tmp_path / "f.csv", n_rows=500, fraud_ratio=0.02, seed=7)
        profile = data.describe_csv(p)

        assert profile["row_count"] == 500
        meta = profile["metadata"]
        assert len(meta["columns"]) == 31
        assert [c["name"] for c in meta["columns"]] == data.CANONICAL_COLUMNS
        assert meta["target"] == "class"
        assert meta["missing_values"] == 0
        assert 0 < meta["fraud_ratio"] < 1
        assert meta["n_fraud"] == round(meta["fraud_ratio"] * 500)
