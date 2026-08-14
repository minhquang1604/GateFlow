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
