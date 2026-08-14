"""Paging and query-count tests for the two list endpoints that had
neither.

``GET /datasets`` and ``GET /models`` returned every row and then ran
two more queries per row to fill in the summary fields — 2N+1, unbounded,
on the console's landing path for those pages.

The query counts below are asserted against a real SQLAlchemy event
listener rather than eyeballed, because "we removed the N+1" is exactly
the kind of claim that quietly stops being true. They are upper bounds:
the point is that the number does not grow with the number of rows.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import event

from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelState, ModelVersion


@contextmanager
def count_queries(engine):
    """Count SELECTs issued on ``engine`` inside the block."""
    seen: list[str] = []

    def _before(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _seed_datasets(session_factory, n: int, versions_each: int = 3):
    s = session_factory()
    try:
        for i in range(n):
            ds = Dataset(name=f"ds-{i}")
            s.add(ds)
            s.flush()
            for v in range(1, versions_each + 1):
                s.add(
                    DatasetVersion(
                        dataset_id=ds.id,
                        version_number=v,
                        storage_uri=f"s3://b/{i}-v{v}.csv",
                        row_count=100 * v,
                        checksum=f"c{i}{v}",
                        schema_hash=f"h{i}{v}",
                    )
                )
        s.commit()
    finally:
        s.close()


def _seed_models(session_factory, n: int, versions_each: int = 3):
    """``n`` models, the newest version of each in PRODUCTION.

    ModelVersion.dataset_version_id is NOT NULL — every model version is
    trained on some dataset version — so one shared DatasetVersion is
    created here to hang them off.
    """
    s = session_factory()
    try:
        ds = Dataset(name="models-fixture-dataset")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://b/v1.csv",
            row_count=10, checksum="c", schema_hash="h",
        )
        s.add(dv)
        s.flush()

        for i in range(n):
            m = ModelRow(name=f"m-{i}")
            s.add(m)
            s.flush()
            for v in range(1, versions_each + 1):
                s.add(
                    ModelVersion(
                        model_id=m.id,
                        dataset_version_id=dv.id,
                        version_number=v,
                        state=(
                            ModelState.PRODUCTION.value
                            if v == versions_each
                            else ModelState.ARCHIVED.value
                        ),
                    )
                )
        s.commit()
    finally:
        s.close()


class TestDatasetsPaging:
    def test_limit_and_offset(self, client, session_factory):
        _seed_datasets(session_factory, 5)

        page1 = client.get("/api/datasets?limit=2").json()
        page2 = client.get("/api/datasets?limit=2&offset=2").json()
        assert [d["name"] for d in page1] == ["ds-0", "ds-1"]
        assert [d["name"] for d in page2] == ["ds-2", "ds-3"]

    def test_total_count_header_is_the_unpaged_total(self, client, session_factory):
        _seed_datasets(session_factory, 5)
        r = client.get("/api/datasets?limit=2")
        assert len(r.json()) == 2
        assert r.headers["X-Total-Count"] == "5"

    def test_summary_fields_survive_the_rewrite(self, client, session_factory):
        _seed_datasets(session_factory, 2, versions_each=4)
        body = client.get("/api/datasets").json()
        assert all(d["version_count"] == 4 for d in body)
        # latest = highest version_number, not "whatever came back first"
        assert all(d["latest_version"]["version_number"] == 4 for d in body)

    def test_dataset_with_no_versions_still_listed(self, client, session_factory):
        _seed_datasets(session_factory, 1, versions_each=0)
        body = client.get("/api/datasets").json()
        assert len(body) == 1
        assert body[0]["version_count"] == 0
        assert body[0]["latest_version"] is None

    def test_empty_is_empty(self, client):
        r = client.get("/api/datasets")
        assert r.json() == []
        assert r.headers["X-Total-Count"] == "0"

    @pytest.mark.parametrize("n", [2, 12])
    def test_query_count_does_not_grow_with_rows(self, client, session_factory, engine, n):
        _seed_datasets(session_factory, n)
        with count_queries(engine) as seen:
            assert len(client.get("/api/datasets").json()) == n
        assert len(seen) <= 4, f"{len(seen)} queries for {n} datasets:\n" + "\n".join(seen)


class TestModelsPaging:
    def test_limit_and_offset(self, client, session_factory):
        _seed_models(session_factory, 5)

        page1 = client.get("/api/models?limit=2").json()
        page2 = client.get("/api/models?limit=2&offset=2").json()
        assert [m["name"] for m in page1] == ["m-0", "m-1"]
        assert [m["name"] for m in page2] == ["m-2", "m-3"]

    def test_total_count_header_is_the_unpaged_total(self, client, session_factory):
        _seed_models(session_factory, 5)
        r = client.get("/api/models?limit=2")
        assert len(r.json()) == 2
        assert r.headers["X-Total-Count"] == "5"

    def test_summary_fields_survive_the_rewrite(self, client, session_factory):
        _seed_models(session_factory, 2, versions_each=3)
        body = client.get("/api/models").json()
        assert all(m["version_count"] == 3 for m in body)
        assert all(m["production_version"]["version_number"] == 3 for m in body)

    def test_model_with_no_versions_still_listed(self, client, session_factory):
        _seed_models(session_factory, 1, versions_each=0)
        body = client.get("/api/models").json()
        assert len(body) == 1
        assert body[0]["version_count"] == 0
        assert body[0]["production_version"] is None

    @pytest.mark.parametrize("n", [2, 12])
    def test_query_count_does_not_grow_with_rows(self, client, session_factory, engine, n):
        _seed_models(session_factory, n)
        with count_queries(engine) as seen:
            assert len(client.get("/api/models").json()) == n
        assert len(seen) <= 4, f"{len(seen)} queries for {n} models:\n" + "\n".join(seen)


class TestBounds:
    """The same bounds runs.py already uses — a client cannot ask for an
    unbounded page, which is what made the old endpoint a problem."""

    @pytest.mark.parametrize("path", ["/api/datasets", "/api/models"])
    def test_limit_is_capped(self, client, path):
        assert client.get(f"{path}?limit=1001").status_code == 422

    @pytest.mark.parametrize("path", ["/api/datasets", "/api/models"])
    def test_limit_must_be_positive(self, client, path):
        assert client.get(f"{path}?limit=0").status_code == 422

    @pytest.mark.parametrize("path", ["/api/datasets", "/api/models"])
    def test_offset_must_not_be_negative(self, client, path):
        assert client.get(f"{path}?offset=-1").status_code == 422
