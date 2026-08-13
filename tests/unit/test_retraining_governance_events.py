"""GovernanceEvent coverage for the three RetrainingWorkflow.run()
branches api/routers/internal.py's own call sites don't reach:
readiness-blocked (retraining.py's own copy, not internal.py's),
eligibility-not-eligible, and drift-detected.

TrainingFailedEvent's two branches inside retraining.py (training
raised / training ended non-SUCCESS) are not covered here — reaching
them needs a real TrainingService + orchestrator, already exercised
end-to-end by tests/api/test_audit_api.py's run-now tests for the
audit side; the event-write call is the same one-call shape as every
other branch in this file, code-reviewed rather than re-proven here.

``training_service=None`` is safe in every test below: none of these
branches call ``self._service`` — they all return before step 4.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.database.base import Base
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.governance_event import GovernanceEvent
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.drift.detector import DriftService, ScipyDriftDetector
from mlops_framework.governance.eligibility import EligibilityConfig
from mlops_framework.readiness.engine import TrainingPolicy
from mlops_framework.workflow.retraining import RetrainingWorkflow


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _seed(session, *, two_versions: bool = False) -> dict:
    ds = Dataset(name="governance-events")
    session.add(ds)
    session.flush()
    v1 = DatasetVersion(
        dataset_id=ds.id, version_number=1, storage_uri="s3://x/v1.csv",
        checksum="a" * 64, schema_hash="b" * 64, row_count=1000,
    )
    session.add(v1)
    session.flush()
    ids = {"dataset_id": ds.id, "v1_id": v1.id}
    if two_versions:
        v2 = DatasetVersion(
            dataset_id=ds.id, version_number=2, storage_uri="s3://x/v2.csv",
            checksum="c" * 64, schema_hash="d" * 64, row_count=1000,
        )
        session.add(v2)
        session.flush()
        ids["v2_id"] = v2.id
    model = ModelRow(name="governance-events-model", task="classification")
    session.add(model)
    session.flush()
    ids["model_id"] = model.id
    session.commit()
    return ids


class TestReadinessBlocked:
    def test_persists_run_blocked_event(self, session):
        ids = _seed(session)
        version = session.get(DatasetVersion, ids["v1_id"])
        model = session.get(ModelRow, ids["model_id"])
        wf = RetrainingWorkflow(session, training_service=None)

        wf.run(
            dataset_version=version,
            model=model,
            training_policy=TrainingPolicy(required_size=999_999),
        )

        events = session.query(GovernanceEvent).all()
        assert len(events) == 1
        assert events[0].event_type == "RUN_BLOCKED"
        assert events[0].entity_type == "DatasetVersion"
        assert events[0].entity_id == version.id
        assert json.loads(events[0].payload_json)["reason"] == "readiness_blocked"


class TestEligibilityNotEligible:
    def test_persists_run_blocked_event(self, session):
        """require_drift_to_retrain only rejects once a drift *result*
        exists (``context.drift is not None`` — see eligibility.py) — an
        unrun drift check is treated as unknown, not "no drift", so this
        needs a real DriftService call that comes back drift_detected=False,
        not just the config flag on its own."""
        ids = _seed(session, two_versions=True)
        current_version = session.get(DatasetVersion, ids["v2_id"])
        model = session.get(ModelRow, ids["model_id"])
        drift_service = DriftService(session, ScipyDriftDetector())
        wf = RetrainingWorkflow(session, training_service=None, drift_service=drift_service)

        # Identical distributions -> the KS-test cannot find drift.
        identical = [1.0, 1.1, 0.9, 1.05, 0.95] * 20
        reference_data = {"amount": list(identical)}
        current_data = {"amount": list(identical)}

        wf.run(
            dataset_version=current_version,
            model=model,
            eligibility_config=EligibilityConfig(require_drift_to_retrain=True),
            reference_data=reference_data,
            current_data=current_data,
        )

        events = session.query(GovernanceEvent).all()
        blocked = [e for e in events if e.event_type == "RUN_BLOCKED"]
        assert len(blocked) == 1
        assert blocked[0].entity_type == "Model"
        assert blocked[0].entity_id == model.id
        assert json.loads(blocked[0].payload_json)["reason"] == "not_eligible"


class TestDriftDetected:
    def test_persists_drift_detected_event(self, session):
        ids = _seed(session, two_versions=True)
        current_version = session.get(DatasetVersion, ids["v2_id"])
        model = session.get(ModelRow, ids["model_id"])
        drift_service = DriftService(session, ScipyDriftDetector())
        wf = RetrainingWorkflow(session, training_service=None, drift_service=drift_service)

        # Wildly different distributions -> a real KS-test finds drift.
        reference_data = {"amount": [1.0, 1.1, 0.9, 1.05, 0.95] * 20}
        current_data = {"amount": [100.0, 105.0, 95.0, 110.0, 90.0] * 20}

        # block_when_drift_detected makes eligibility reject right after —
        # deliberately, so run() returns before step 4 (training), which
        # would AttributeError against training_service=None. The
        # DRIFT_DETECTED event this test cares about is already persisted
        # by the time that happens (step 2, ahead of eligibility).
        wf.run(
            dataset_version=current_version,
            model=model,
            eligibility_config=EligibilityConfig(block_when_drift_detected=True),
            reference_data=reference_data,
            current_data=current_data,
        )

        events = session.query(GovernanceEvent).all()
        drift_events = [e for e in events if e.event_type == "DRIFT_DETECTED"]
        assert len(drift_events) == 1
        assert drift_events[0].severity == "CRITICAL"
        assert drift_events[0].entity_type == "DatasetVersion"
        assert drift_events[0].entity_id == current_version.id
