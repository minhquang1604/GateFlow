"""Tests for MLOpsProject.report() / sdk/report.py::build_report.

Seeds every table a report reads from directly against the project's
own DatabaseManager (there is no SDK-level ``create_model_version`` —
that only exists via the training flow, which is heavier than this
report-composition test needs) and asserts the rendered output surfaces
each section's real data, not just that it doesn't crash.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.audit.manager import AuditManager
from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import (
    DriftEvaluation,
    DriftOutcome,
)
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_promotion_event import (
    ModelPromotionEvent,
    ModelPromotionStatus,
)
from mlops_framework.database.models.model_version import ModelState, ModelVersion
from mlops_framework.database.models.readiness_evaluation import (
    ReadinessEvaluation,
    ReadinessStatus,
)
from mlops_framework.database.models.training_run import RunStatus, TrainingRun
from mlops_framework.database.session import DatabaseManager
from mlops_framework.events.publisher import TrainingFailedEvent
from mlops_framework.events.store import GovernanceEventStore
from mlops_framework.sdk import MLOpsProject
from mlops_framework.sdk.exceptions import NotFoundError


@pytest.fixture()
def project():
    """Build an MLOpsProject backed by an isolated in-memory SQLite DB.

    Mirrors test_project.py's fixture exactly — not shared via a
    conftest.py because that file doesn't define one; kept local rather
    than adding shared test infrastructure for one more consumer.
    """
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

    p = MLOpsProject("test", db_manager=mgr)
    yield p

    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed(project) -> int:
    """Seed one fully-populated ModelVersion — dataset, training run,
    readiness/drift/promotion history, one audit row, one alert — and
    return its id."""
    with project._db.get_session() as s:
        ds = Dataset(name="fraud")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://bucket/v1.parquet",
            checksum="a" * 64, schema_hash="b" * 64, row_count=284_807,
        )
        s.add(dv)
        s.flush()
        run = TrainingRun(
            dataset_version_id=dv.id, status=RunStatus.SUCCESS.value,
            pipeline_id="case_studies.fraud_detection.pipelines:train_xgboost",
        )
        s.add(run)
        s.flush()
        model = ModelRow(name="fraud-xgboost-report-test", task="classification")
        s.add(model)
        s.flush()
        mv = ModelVersion(
            model_id=model.id, dataset_version_id=dv.id, training_run_id=run.id,
            version_number=1, state=ModelState.PRODUCTION,
            metrics_json=json.dumps({"f1": 0.91, "average_precision": 0.87}),
            artifact_uri="s3://bucket/model.pkl",
        )
        s.add(mv)
        s.flush()

        s.add(ReadinessEvaluation(
            dataset_version_id=dv.id, status=ReadinessStatus.READY, reasons_json="[]",
        ))
        s.add(DriftEvaluation(
            reference_dataset_version_id=dv.id, current_dataset_version_id=dv.id,
            method="ks", outcome=DriftOutcome.NO_DRIFT, score=0.02, threshold=0.05,
        ))
        s.add(ModelPromotionEvent(
            event_type="MODEL_PROMOTED", model_id=model.id, model_version_id=mv.id,
            model_name=model.name, model_version_number=mv.version_number,
            status=ModelPromotionStatus.PUBLISHED,
        ))
        AuditManager(s).record(
            actor="alice@example.com", action="MODEL_PROMOTED",
            entity_type="ModelVersion", entity_id=mv.id,
        )
        GovernanceEventStore(s).record(
            TrainingFailedEvent(training_run_id=999, error_message="a prior attempt failed"),
            message="A prior training run for this dataset failed",
            entity_type="TrainingRun", entity_id=run.id,
        )
        return mv.id


class TestReportMarkdown:
    def test_contains_every_section_with_real_data(self, project):
        mv_id = _seed(project)
        report = project.report(mv_id)

        assert "# Reproducibility report — fraud-xgboost-report-test v1" in report
        assert "PRODUCTION" in report
        assert "s3://bucket/model.pkl" in report
        # Metrics
        assert "0.91" in report
        assert "average_precision" in report
        # Dataset content hash — the whole point of this section
        assert "a" * 64 in report
        assert "b" * 64 in report
        assert "284,807" in report
        # Training run
        assert "case_studies.fraud_detection.pipelines:train_xgboost" in report
        assert "SUCCESS" in report
        # Lineage
        assert "DatasetVersion" in report
        assert "TrainingRun" in report
        assert "ModelVersion" in report
        # Decision trail
        assert "READY" in report
        assert "NO_DRIFT" in report
        assert "PUBLISHED" in report
        # Audit + alerts
        assert "alice@example.com" in report
        assert "A prior training run for this dataset failed" in report

    def test_unknown_model_version_raises_not_found(self, project):
        with pytest.raises(NotFoundError):
            project.report(999_999)

    def test_unsupported_format_raises(self, project):
        mv_id = _seed(project)
        with pytest.raises(ValueError):
            project.report(mv_id, format="pdf")


class TestReportHtml:
    def test_wraps_markdown_body_in_a_self_contained_page(self, project):
        mv_id = _seed(project)
        html = project.report(mv_id, format="html")
        assert html.startswith("<!doctype html>")
        assert "<title>" in html
        assert "fraud-xgboost-report-test" in html
        # The markdown content is preserved (escaped) inside <pre>.
        assert "Reproducibility report" in html
