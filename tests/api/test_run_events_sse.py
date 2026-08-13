"""Tests for GET /api/training-runs/{id}/events — the SSE status stream.

``_SSE_POLL_SECONDS``/``_SSE_MAX_SECONDS`` are monkeypatched down to
milliseconds in every test here — the real defaults (2s / 30min) would
make this file take half an hour to run. The polling *logic* under test
is identical either way; only the interval changes.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from mlops_framework.api.routers import runs as runs_module
from mlops_framework.database.models.dataset import Dataset
from mlops_framework.database.models.dataset_version import DatasetVersion
from mlops_framework.database.models.training_run import RunStatus, TrainingRun


def _seed_run(session_factory, *, status: str) -> int:
    s = session_factory()
    try:
        ds = Dataset(name="d")
        s.add(ds)
        s.flush()
        dv = DatasetVersion(
            dataset_id=ds.id, version_number=1, storage_uri="s3://x/v1.csv",
            checksum="a" * 64, schema_hash="b" * 64, row_count=10,
        )
        s.add(dv)
        s.flush()
        run = TrainingRun(dataset_version_id=dv.id, status=status, pipeline_id="p")
        s.add(run)
        s.flush()
        s.commit()
        return run.id
    finally:
        s.close()


def _set_status(session_factory, run_id: int, status: str) -> None:
    s = session_factory()
    try:
        run = s.get(TrainingRun, run_id)
        run.status = status
        s.commit()
    finally:
        s.close()


def _parse_events(lines: list[str]) -> list[tuple[str, dict]]:
    """SSE frames are `event: X` then `data: {...}` then a blank line."""
    events: list[tuple[str, dict]] = []
    event_name = None
    for line in lines:
        if line.startswith("event: "):
            event_name = line[len("event: "):]
        elif line.startswith("data: "):
            events.append((event_name, json.loads(line[len("data: "):])))
    return events


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(runs_module, "_SSE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(runs_module, "_SSE_MAX_SECONDS", 0.2)


class TestUnknownRun:
    def test_404_before_the_stream_even_opens(self, client):
        resp = client.get("/api/training-runs/9999/events")
        assert resp.status_code == 404


class TestAlreadyTerminal:
    def test_one_event_then_closes_immediately(self, client, session_factory):
        run_id = _seed_run(session_factory, status=RunStatus.SUCCESS.value)
        with client.stream("GET", f"/api/training-runs/{run_id}/events") as resp:
            assert resp.status_code == 200
            events = _parse_events(list(resp.iter_lines()))
        assert len(events) == 1
        name, data = events[0]
        assert name == "status"
        assert data["status"] == "SUCCESS"
        assert data["id"] == run_id


class TestStatusTransition:
    def test_emits_one_event_per_change_in_order(self, client, session_factory):
        run_id = _seed_run(session_factory, status=RunStatus.PENDING.value)

        def _flip():
            time.sleep(0.05)
            _set_status(session_factory, run_id, RunStatus.RUNNING.value)
            time.sleep(0.05)
            _set_status(session_factory, run_id, RunStatus.SUCCESS.value)

        t = threading.Thread(target=_flip, daemon=True)
        t.start()
        try:
            with client.stream("GET", f"/api/training-runs/{run_id}/events") as resp:
                events = _parse_events(list(resp.iter_lines()))
        finally:
            t.join(timeout=2)

        statuses = [data["status"] for name, data in events if name == "status"]
        # PENDING (the initial event), then RUNNING, then SUCCESS — never
        # a repeat of the same status back-to-back, and the stream must
        # not have closed on the RUNNING event (that had already
        # happened before the SUCCESS one, so anything less means the
        # loop stopped watching too early).
        assert statuses == ["PENDING", "RUNNING", "SUCCESS"]

    def test_no_event_emitted_when_status_is_unchanged(self, client, session_factory):
        """Multiple poll ticks over an unchanging RUNNING status must not
        each produce their own event — only transitions do."""
        run_id = _seed_run(session_factory, status=RunStatus.RUNNING.value)

        def _flip_after_a_few_ticks():
            time.sleep(0.08)  # several poll intervals at 0.02s each
            _set_status(session_factory, run_id, RunStatus.SUCCESS.value)

        t = threading.Thread(target=_flip_after_a_few_ticks, daemon=True)
        t.start()
        try:
            with client.stream("GET", f"/api/training-runs/{run_id}/events") as resp:
                events = _parse_events(list(resp.iter_lines()))
        finally:
            t.join(timeout=2)

        statuses = [data["status"] for name, data in events if name == "status"]
        assert statuses == ["RUNNING", "SUCCESS"]


class TestTimeout:
    def test_closes_with_a_timeout_event_if_never_terminal(self, client, session_factory):
        run_id = _seed_run(session_factory, status=RunStatus.RUNNING.value)
        # Never flips to a terminal status — _SSE_MAX_SECONDS=0.2s must
        # still end the stream on its own rather than holding the
        # connection open forever.
        started = time.monotonic()
        with client.stream("GET", f"/api/training-runs/{run_id}/events") as resp:
            events = _parse_events(list(resp.iter_lines()))
        elapsed = time.monotonic() - started

        names = [name for name, _ in events]
        assert names[0] == "status"
        assert names[-1] == "timeout"
        assert elapsed < 5, "stream did not close on its own within a sane bound"
