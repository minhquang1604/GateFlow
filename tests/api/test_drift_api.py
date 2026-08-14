"""Tests for ``GET /api/drift/{version_id}``.

This router had no API-level test at all — ``tests/unit/test_drift.py``
covers the detector, but nothing covered the endpoint the console's
dataset-detail drift panel actually calls. Two behaviours it has that
are easy to get wrong and were unpinned:

* a version can sit on *either* side of a comparison (reference or
  current), and the endpoint has to find it either way;
* "no evaluation yet" is ``null`` with a 200, not a 404 — the same
  convention ``GET /readiness/{version_id}`` uses, because the console
  renders an empty panel rather than treating it as an error. A 404 is
  reserved for a dataset version that genuinely does not exist.
"""

from __future__ import annotations

import json

import pytest

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.drift_evaluation import (
    DriftEvaluation,
    DriftOutcome,
)


@pytest.fixture()
def two_versions(session_factory):
    """One dataset, two versions — v1 as reference, v2 as current."""
    s = session_factory()
    try:
        ds = Dataset(name="churn")
        s.add(ds)
        s.flush()
        versions = []
        for n in (1, 2):
            v = DatasetVersion(
                dataset_id=ds.id,
                version_number=n,
                storage_uri=f"s3://b/v{n}.csv",
                row_count=1000 * n,
                checksum=f"c{n}",
                schema_hash=f"h{n}",
            )
            s.add(v)
            versions.append(v)
        s.commit()
        return [v.id for v in versions]
    finally:
        s.close()


def _record_drift(session_factory, ref_id, cur_id, **kwargs):
    s = session_factory()
    try:
        row = DriftEvaluation(
            reference_dataset_version_id=ref_id,
            current_dataset_version_id=cur_id,
            method=kwargs.get("method", "ks"),
            outcome=kwargs.get("outcome", DriftOutcome.DRIFT_DETECTED),
            score=kwargs.get("score", 0.0123),
            threshold=kwargs.get("threshold", 0.05),
            details_json=json.dumps(kwargs.get("details", {"v1": {"score": 0.01}})),
        )
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


class TestNoEvaluationYet:
    def test_returns_null_not_404(self, client, two_versions):
        r = client.get(f"/api/drift/{two_versions[0]}")
        assert r.status_code == 200
        assert r.json() is None

    def test_unknown_version_is_404(self, client):
        r = client.get("/api/drift/9999")
        assert r.status_code == 404
        assert "9999" in r.json()["detail"]


class TestFindsTheEvaluationFromEitherSide:
    def test_found_when_version_is_the_current_side(self, client, session_factory, two_versions):
        ref, cur = two_versions
        _record_drift(session_factory, ref, cur)

        body = client.get(f"/api/drift/{cur}").json()
        assert body is not None
        assert body["current_dataset_version_id"] == cur
        assert body["reference_dataset_version_id"] == ref

    def test_found_when_version_is_the_reference_side(self, client, session_factory, two_versions):
        ref, cur = two_versions
        _record_drift(session_factory, ref, cur)

        body = client.get(f"/api/drift/{ref}").json()
        assert body is not None
        assert body["reference_dataset_version_id"] == ref


class TestPayload:
    def test_reports_the_verdict_and_its_numbers(self, client, session_factory, two_versions):
        ref, cur = two_versions
        _record_drift(
            session_factory, ref, cur,
            method="ks", score=0.0123, threshold=0.05,
            outcome=DriftOutcome.DRIFT_DETECTED,
        )

        body = client.get(f"/api/drift/{cur}").json()
        assert body["method"] == "ks"
        assert body["score"] == pytest.approx(0.0123)
        assert body["threshold"] == pytest.approx(0.05)
        assert body["outcome"] == DriftOutcome.DRIFT_DETECTED.value

    def test_per_feature_details_are_parsed_not_returned_as_a_string(
        self, client, session_factory, two_versions
    ):
        ref, cur = two_versions
        _record_drift(
            session_factory, ref, cur,
            details={"v1": {"score": 0.01, "drift_detected": True}},
        )

        body = client.get(f"/api/drift/{cur}").json()
        assert body["details"] == {"v1": {"score": 0.01, "drift_detected": True}}

    def test_no_drift_outcome_round_trips(self, client, session_factory, two_versions):
        ref, cur = two_versions
        _record_drift(session_factory, ref, cur, outcome=DriftOutcome.NO_DRIFT)

        body = client.get(f"/api/drift/{cur}").json()
        assert body["outcome"] == DriftOutcome.NO_DRIFT.value


class TestMostRecentWins:
    def test_returns_the_latest_evaluation(self, client, session_factory, two_versions):
        ref, cur = two_versions
        _record_drift(session_factory, ref, cur, score=0.10, outcome=DriftOutcome.NO_DRIFT)
        newest = _record_drift(
            session_factory, ref, cur, score=0.99, outcome=DriftOutcome.DRIFT_DETECTED
        )

        body = client.get(f"/api/drift/{cur}").json()
        assert body["id"] == newest
        assert body["score"] == pytest.approx(0.99)


class TestReadsAreNotGated:
    def test_no_write_token_needed(self, anon_client, two_versions):
        assert anon_client.get(f"/api/drift/{two_versions[0]}").status_code == 200


# ---------------------------------------------------------------------- #
# POST /api/drift/{version_id}/check — trigger an Airflow drift run
# ---------------------------------------------------------------------- #


@pytest.fixture()
def airflow_env(monkeypatch):
    from mlops_framework.config.settings import get_settings

    monkeypatch.setenv("AIRFLOW_BASE_URL", "http://airflow:8080")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def fake_orchestrator(monkeypatch):
    """Capture what the endpoint asks Airflow to run."""
    triggered = {}

    class _Fake:
        def __init__(self, **kwargs):
            triggered["auth"] = (kwargs.get("username"), kwargs.get("password"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            triggered["closed"] = True

        def trigger_pipeline(self, dag_id, config=None):
            triggered["dag_id"] = dag_id
            triggered["conf"] = config
            return f"{dag_id}/run-abc123"

    monkeypatch.setattr(
        "mlops_framework.orchestration.airflow.AirflowOrchestrator", _Fake
    )
    return triggered


class TestTriggerDriftCheck:
    def test_queues_the_dag_against_the_previous_version(
        self, client, two_versions, airflow_env, fake_orchestrator
    ):
        v1, v2 = two_versions
        r = client.post(f"/api/drift/{v2}/check", json={})

        # 202: the verdict is not ready yet, only the run is queued.
        assert r.status_code == 202
        body = r.json()
        assert body["dag_id"] == "mlops_drift_check"
        assert body["reference_dataset_version_id"] == v1
        assert body["current_dataset_version_id"] == v2
        assert body["execution_id"] == "mlops_drift_check/run-abc123"

        assert fake_orchestrator["conf"] == {
            "reference_version_id": v1,
            "current_version_id": v2,
            "sample_size": 5000,
        }
        assert fake_orchestrator["closed"] is True

    def test_explicit_reference_is_honoured(
        self, client, two_versions, airflow_env, fake_orchestrator
    ):
        v1, v2 = two_versions
        r = client.post(
            f"/api/drift/{v1}/check", json={"reference_version_id": v2, "sample_size": 250}
        )
        assert r.status_code == 202
        assert fake_orchestrator["conf"]["reference_version_id"] == v2
        assert fake_orchestrator["conf"]["sample_size"] == 250

    def test_first_version_has_nothing_to_compare_against(
        self, client, two_versions, airflow_env, fake_orchestrator
    ):
        v1, _ = two_versions
        r = client.post(f"/api/drift/{v1}/check", json={})
        assert r.status_code == 422
        assert "first version" in r.json()["detail"]
        assert "dag_id" not in fake_orchestrator

    def test_cannot_compare_a_version_with_itself(
        self, client, two_versions, airflow_env, fake_orchestrator
    ):
        v1, v2 = two_versions
        r = client.post(f"/api/drift/{v2}/check", json={"reference_version_id": v2})
        assert r.status_code == 422
        assert "itself" in r.json()["detail"]

    def test_unknown_version_is_404(self, client, airflow_env, fake_orchestrator):
        assert client.post("/api/drift/9999/check", json={}).status_code == 404

    def test_without_airflow_configured_it_says_so(
        self, client, two_versions, monkeypatch
    ):
        from mlops_framework.config.settings import get_settings

        monkeypatch.delenv("AIRFLOW_BASE_URL", raising=False)
        get_settings.cache_clear()
        r = client.post(f"/api/drift/{two_versions[1]}/check", json={})
        assert r.status_code == 503
        assert "AIRFLOW_BASE_URL" in r.json()["detail"]
        get_settings.cache_clear()

    def test_records_who_asked(
        self, client, session_factory, two_versions, airflow_env, fake_orchestrator
    ):
        from mlops_framework.database.models.audit_log import AuditLog

        v1, v2 = two_versions
        client.post(f"/api/drift/{v2}/check", json={}, headers={"X-Actor": "bob"})

        s = session_factory()
        try:
            row = s.query(AuditLog).filter_by(action="DRIFT_CHECK_TRIGGERED").one()
            assert row.actor == "bob"
            assert row.entity_id == v2
            assert json.loads(row.metadata_json)["reference_dataset_version_id"] == v1
        finally:
            s.close()

    def test_is_gated(self, anon_client, two_versions, airflow_env, fake_orchestrator):
        assert anon_client.post(f"/api/drift/{two_versions[1]}/check", json={}).status_code == 401
        assert "dag_id" not in fake_orchestrator
