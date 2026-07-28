"""Tests for the Customer Churn case study.

Same shape as :mod:`case_studies.fraud_detection.tests.test_use_case`
but with churn-specific data and a reusability check that compares the
two case studies' SDK surfaces.
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
from mlops_framework.sdk import (
    AlreadyExistsError,
    MLOpsProject,
    NotFoundError,
)
from mlops_framework.sdk.exceptions import TrainingError

from case_studies.customer_churn import data
from case_studies.customer_churn.pipelines import (
    fail,
    train_balanced,
    train_baseline,
)


# ---------------------------------------------------------------------- #
# Data generator
# ---------------------------------------------------------------------- #


class TestDataGenerator:
    def test_row_count(self):
        rows = list(data.generate(n_rows=100))
        assert len(rows) == 100

    def test_columns(self):
        rows = list(data.generate(n_rows=10))
        assert set(rows[0].keys()) == {
            "customer_id", "tenure_months", "monthly_charges",
            "contract_type", "payment_method", "num_complaints", "churn",
        }

    def test_churn_rate_is_roughly_correct(self):
        rows = list(data.generate(n_rows=10000, seed=1))
        n = sum(1 for r in rows if r["churn"] == 1)
        # Average contract mix produces ~27% churn; allow a wide band
        assert 2200 <= n <= 3200

    def test_month_to_month_churns_more(self):
        rows = list(data.generate(n_rows=20000, seed=2))
        m2m = [r for r in rows if r["contract_type"] == "month-to-month"]
        two_y = [r for r in rows if r["contract_type"] == "two-year"]
        m2m_rate = sum(r["churn"] for r in m2m) / len(m2m)
        two_y_rate = sum(r["churn"] for r in two_y) / len(two_y)
        assert m2m_rate > two_y_rate * 2

    def test_write_csv(self, tmp_path):
        p = data.write_csv(tmp_path / "churn.csv", n_rows=50)
        with p.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 50
        assert "churn" in rows[0]


# ---------------------------------------------------------------------- #
# Pipelines
# ---------------------------------------------------------------------- #


class TestPipelines:
    def test_train_baseline(self):
        out = train_baseline({"training_run_id": 1})
        assert out["status"] == "SUCCESS"
        assert "accuracy" in out["metrics"]
        assert "f1" in out["metrics"]
        assert Path(out["artifact_path"]).exists()

    def test_train_balanced(self):
        out = train_balanced({"training_run_id": 1})
        assert "recall" in out["metrics"]

    def test_fail_raises(self):
        with pytest.raises(RuntimeError):
            fail({})


# ---------------------------------------------------------------------- #
# SDK lifecycle
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
    from mlops_framework.orchestration.local import LocalDockerOrchestrator
    from mlops_framework.tracking.in_memory import InMemoryTracker
    orch = LocalDockerOrchestrator()
    p = MLOpsProject(
        "churn",
        db_manager=mgr,
        orchestrator=orch,
        tracker=InMemoryTracker(),
    )
    p.register_pipeline("baseline", "case_studies.customer_churn.pipelines:train_baseline")
    p.register_pipeline("balanced", "case_studies.customer_churn.pipelines:train_balanced")
    p.register_pipeline("fail", "case_studies.customer_churn.pipelines:fail")
    try:
        yield p
    finally:
        orch.shutdown()
        Base.metadata.drop_all(engine)
        engine.dispose()


class TestCaseStudyLifecycle:
    def test_full_lifecycle(self, project, tmp_path):
        csv_path = data.write_csv(tmp_path / "churn.csv", n_rows=2000)
        ds = project.create_dataset("telco", description="telecom")
        v = ds.create_version(
            storage_uri=str(csv_path),
            row_count=2000,
            metadata=data.schema_metadata(),
        )
        m = project.create_model("churn-clf", task="binary_classification")
        run = project.train(
            dataset_version=v, pipeline="baseline", wait=True, timeout=30
        )
        assert run.status == "SUCCESS"
        run2 = project.train(
            dataset_version=v, pipeline="balanced", wait=True, timeout=30
        )
        assert run2.status == "SUCCESS"
        g = project.lineage.for_dataset_version(v.id)
        types = {n["type"] for n in g["nodes"]}
        assert {"Dataset", "DatasetVersion", "TrainingRun"} <= types

    def test_failing_pipeline_raises(self, project, tmp_path):
        csv_path = data.write_csv(tmp_path / "churn.csv", n_rows=100)
        ds = project.create_dataset("ds")
        v = ds.create_version(str(csv_path), 100, metadata=data.schema_metadata())
        with pytest.raises(TrainingError):
            project.train(dataset_version=v, pipeline="fail", wait=True, timeout=10)


# ---------------------------------------------------------------------- #
# Static check: app.py uses only the SDK
# ---------------------------------------------------------------------- #


class TestNoDirectManagerImports:
    def _imports(self, file: Path) -> set[str]:
        tree = ast.parse(file.read_text())
        return {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }

    def test_app_uses_only_sdk(self):
        here = Path(__file__).parent.parent
        app = here / "app.py"
        imports = self._imports(app)
        allowed = {"mlops_framework.sdk", "case_studies.customer_churn"}
        leaked = {
            m for m in imports
            if m and m.startswith("mlops_framework.") and m not in allowed
        }
        assert not leaked, f"app.py imports non-SDK internals: {leaked}"

    def test_pipelines_dont_import_framework(self):
        here = Path(__file__).parent.parent
        p = here / "pipelines.py"
        imports = self._imports(p)
        framework_imports = {m for m in imports if m and m.startswith("mlops_framework")}
        assert not framework_imports


# ---------------------------------------------------------------------- #
# Reusability proof: the two case studies share the same SDK surface
# ---------------------------------------------------------------------- #


class TestReusabilityAcrossCaseStudies:
    """Both case studies must use the same public SDK — no custom hooks."""

    def test_same_sdk_exports(self):
        from mlops_framework import sdk as fraud_sdk
        from mlops_framework import sdk as churn_sdk  # same module
        # The SDK is one module; both case studies see the same exports.
        assert fraud_sdk is churn_sdk

    def test_app_modules_have_same_import_shape(self):
        here = Path(__file__).parent.parent.parent
        fraud_app = here / "fraud_detection" / "app.py"
        churn_app = here / "customer_churn" / "app.py"

        def from_imports(p: Path) -> set[str]:
            tree = ast.parse(p.read_text())
            return {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }

        # Both apps import exactly one thing from mlops_framework
        fraud_ml = {m for m in from_imports(fraud_app) if m.startswith("mlops_framework")}
        churn_ml = {m for m in from_imports(churn_app) if m.startswith("mlops_framework")}
        assert fraud_ml == churn_ml == {"mlops_framework.sdk"}
