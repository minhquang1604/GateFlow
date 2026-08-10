"""AirflowOrchestrator against a real Airflow deployment.

Opt-in: skipped unless ``AIRFLOW_BASE_URL`` points at a reachable
Airflow 2.x whose REST API accepts basic auth::

    AIRFLOW_BASE_URL=http://<host>:8080 \
    AIRFLOW_USERNAME=admin AIRFLOW_PASSWORD=... \
    pytest tests/integration/test_airflow_live.py

Why this file exists
--------------------

The fake-client unit tests assert the adapter *sends* a particular URL —
only a real server can say whether that URL is one Airflow actually
*serves*. The adapter spent its whole life addressing DAG runs at
``/api/v1/dagRuns/{id}``, which Airflow 2.x does not serve; the fake
answered 200 to it and the suite stayed green for months.
:class:`TestLegacyRouteIsGone` pins that fact against a live server.

Safety
------

Every DAG run this module creates is deleted again in teardown, and the
tests that would actually *execute* tasks are skipped when the target
DAG is paused. Pointed at the deployed stack — where
``mlops_training_pipeline`` is paused — it exercises trigger, read,
cancel and the error paths without a single task running, so nothing
reaches the model registry.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from mlops_framework.exceptions import (
    ExecutionNotFoundError,
    OrchestratorConfigError,
)
from mlops_framework.orchestration.airflow import AirflowOrchestrator
from mlops_framework.orchestration.base import ExecutionState

BASE_URL = os.environ.get("AIRFLOW_BASE_URL", "")
USERNAME = os.environ.get("AIRFLOW_USERNAME", "airflow")
PASSWORD = os.environ.get("AIRFLOW_PASSWORD", "airflow")
# Any DAG registered on the target works — the adapter is agnostic to
# what the DAG does. Defaults to the one the framework ships.
DAG_ID = os.environ.get("AIRFLOW_LIVE_DAG", "mlops_training_pipeline")


def _reachable() -> bool:
    if not BASE_URL:
        return False
    try:
        return httpx.get(f"{BASE_URL.rstrip('/')}/health", timeout=8.0).status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason="set AIRFLOW_BASE_URL to a running Airflow to run the live suite",
)


@pytest.fixture(scope="module")
def orch():
    with AirflowOrchestrator(
        base_url=BASE_URL, username=USERNAME, password=PASSWORD
    ) as o:
        yield o


@pytest.fixture(scope="module")
def dag_is_paused(orch) -> bool:
    r = orch._client.get(f"/api/v1/dags/{DAG_ID}")
    if r.status_code != 200:
        pytest.skip(f"DAG {DAG_ID!r} not registered on {BASE_URL}")
    return bool(r.json().get("is_paused"))


@pytest.fixture()
def trigger(orch):
    """Trigger DAG runs and delete every one of them afterwards.

    Teardown uses DELETE deliberately — that is the right tool for
    removing test data, and the wrong one for cancelling a run (see
    ``AirflowOrchestrator.cancel_execution``).
    """
    created: list[str] = []

    def _trigger(config: dict | None = None) -> str:
        execution_id = orch.trigger_pipeline(DAG_ID, config or {})
        created.append(execution_id)
        return execution_id

    yield _trigger

    for execution_id in created:
        try:
            orch._client.delete(orch._dag_run_url(execution_id))
        except Exception:  # pragma: no cover - best effort cleanup
            pass


def _wait_for_state(orch, execution_id, wanted, timeout=180.0, interval=3.0):
    deadline = time.time() + timeout
    status = orch.get_execution_status(execution_id)
    while time.time() < deadline:
        status = orch.get_execution_status(execution_id)
        if status.state in wanted:
            return status
        time.sleep(interval)
    pytest.fail(
        f"{execution_id} stuck in {status.state} after {timeout}s (wanted {wanted})"
    )


class TestAgainstRealAirflow:
    def test_trigger_returns_an_id_that_is_then_readable(self, orch, trigger):
        """The round trip the adapter could never complete before.

        An id the adapter hands back has to be one it can look up
        again — that is the whole contract, and it was broken.
        """
        execution_id = trigger({"probe": "trigger-read"})

        assert execution_id.startswith(f"{DAG_ID}/")
        status = orch.get_execution_status(execution_id)
        assert status.execution_id == execution_id
        assert status.pipeline_id == DAG_ID
        assert status.state is not ExecutionState.UNKNOWN
        assert status.metadata["external_trigger"] is True

    def test_conf_survives_the_round_trip(self, orch, trigger):
        """The config the framework passes must reach the DAG run."""
        conf = {"training_run_id": 424242, "dataset_version_id": 7}
        status = orch.get_execution_status(trigger(conf))
        assert status.metadata["conf"] == conf

    def test_two_triggers_do_not_collide(self, orch, trigger):
        """Airflow 409s on a duplicate dag_run_id."""
        first, second = trigger(), trigger()
        assert first != second
        assert orch.get_execution_status(first).execution_id == first
        assert orch.get_execution_status(second).execution_id == second

    def test_cancel_stops_the_run_without_erasing_it(self, orch, trigger):
        """Cancel must leave the run in Airflow's history.

        The previous implementation issued DELETE, so the run vanished
        from the UI and from any later audit. The read-back below is
        what would have caught that: after a DELETE it 404s.
        """
        execution_id = trigger()

        cancelled = orch.cancel_execution(execution_id)
        assert cancelled.state is ExecutionState.CANCELLED
        assert cancelled.pipeline_id == DAG_ID

        after = orch.get_execution_status(execution_id)
        assert after.state is ExecutionState.FAILED, (
            "Airflow has no cancelled state; the framework maps it, but "
            "the run itself must survive as failed"
        )

    def test_task_instance_states_are_reported(self, orch, trigger):
        """Works even on a paused DAG — Airflow creates the instances."""
        states = orch.get_task_instances(trigger())
        assert isinstance(states, dict)
        if states:
            assert all(isinstance(v, str) for v in states.values())

    def test_unknown_dag_run_raises_not_found(self, orch):
        with pytest.raises(ExecutionNotFoundError):
            orch.get_execution_status(f"{DAG_ID}/definitely-not-a-real-run")

    def test_unknown_dag_raises_config_error(self, orch):
        with pytest.raises(OrchestratorConfigError):
            orch.trigger_pipeline("no_such_dag_exists", {})

    def test_bare_execution_id_is_rejected_before_any_request(self, orch):
        with pytest.raises(ExecutionNotFoundError, match="not addressable"):
            orch.get_execution_status("just-a-run-id")


class TestExecutingDag:
    """Terminal-state coverage — needs a DAG that is allowed to run."""

    def test_run_reaches_a_terminal_state(self, orch, trigger, dag_is_paused):
        if dag_is_paused:
            pytest.skip(
                f"DAG {DAG_ID!r} is paused, so its tasks never execute. "
                "Point AIRFLOW_LIVE_DAG at an unpaused DAG for this test."
            )
        status = _wait_for_state(
            orch,
            trigger(),
            {ExecutionState.SUCCESS, ExecutionState.FAILED},
        )
        assert status.is_terminal
        assert status.started_at is not None
        assert status.finished_at is not None


class TestLegacyRouteIsGone:
    """Pin the actual defect against a live server."""

    def test_old_flat_dagruns_route_404s(self, orch):
        """The route the adapter used to call is not served by Airflow.

        If Airflow ever starts serving it, this fails and the adapter's
        URL choice deserves a fresh look.
        """
        assert orch._client.get("/api/v1/dagRuns/anything").status_code == 404

    def test_nested_route_is_the_one_that_works(self, orch):
        assert orch._client.get(f"/api/v1/dags/{DAG_ID}/dagRuns").status_code == 200
