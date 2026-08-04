"""MLflowTracker against a real MLflow tracking server.

Opt-in: skipped unless ``MLFLOW_TRACKING_URI`` points at a reachable
MLflow server and the ``mlflow`` SDK is installed::

    MLFLOW_TRACKING_URI=http://<host>:5000 \
    pytest tests/integration/test_mlflow_live.py

Why this file exists
--------------------

``tests/unit/test_mlflow_tracker.py`` drives the adapter against a fake
``mlflow`` module installed into ``sys.modules``. That verifies the
adapter *calls* the right functions, but a stub agrees with whatever
the adapter does — it cannot tell you that ``end_run(status="KILLED")``
is a status MLflow accepts, or that params and metrics actually land in
the backing store. Everything asserted here is read back off the server
through :class:`mlflow.tracking.MlflowClient`, not off the adapter.

Runs land in a dedicated experiment (see ``EXPERIMENT``) which is
deleted in teardown, so pointing this at the deployed tracking server
leaves no lasting state.
"""

from __future__ import annotations

import os
import uuid

import pytest

from mlops_framework.exceptions import ExperimentTrackingError
from mlops_framework.tracking.base import RunStatus

mlflow = pytest.importorskip("mlflow", reason="mlflow SDK is not installed")

TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "")
EXPERIMENT = f"mlops-framework-livetest-{uuid.uuid4().hex[:8]}"


def _reachable() -> bool:
    if not TRACKING_URI:
        return False
    try:
        import httpx

        r = httpx.get(f"{TRACKING_URI.rstrip('/')}/health", timeout=8.0)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason="set MLFLOW_TRACKING_URI to a running MLflow server to run the live suite",
)


@pytest.fixture(scope="module")
def client():
    from mlflow.tracking import MlflowClient

    return MlflowClient(tracking_uri=TRACKING_URI)


@pytest.fixture(scope="module", autouse=True)
def _cleanup_experiment(client):
    yield
    exp = client.get_experiment_by_name(EXPERIMENT)
    if exp is not None:
        client.delete_experiment(exp.experiment_id)


@pytest.fixture()
def tracker():
    """A fresh tracker per test, with MLflow's global run state reset.

    ``mlflow.start_run`` writes to process-global state, so a run left
    active by a failing test would leak into the next one. See
    :class:`TestGlobalRunState` — that coupling is the adapter's, not
    the test's.
    """
    from mlops_framework.tracking.mlflow import MLflowTracker

    while mlflow.active_run() is not None:
        mlflow.end_run()
    t = MLflowTracker(tracking_uri=TRACKING_URI, experiment_name=EXPERIMENT)
    yield t
    while mlflow.active_run() is not None:
        mlflow.end_run()


class TestRunLifecycle:
    def test_start_run_creates_a_run_on_the_server(self, tracker, client):
        run_id = tracker.start_run(run_name="live-start", tags={"training_run_id": "1"})

        assert run_id
        run = client.get_run(run_id)          # 404s if the run isn't real
        assert run.info.run_id == run_id
        assert run.data.tags["training_run_id"] == "1"
        assert run.info.status == "RUNNING"

    def test_experiment_is_the_one_requested(self, tracker, client):
        run_id = tracker.start_run(run_name="live-experiment")
        run = client.get_run(run_id)
        exp = client.get_experiment(run.info.experiment_id)
        assert exp.name == EXPERIMENT

    def test_start_run_twice_returns_the_same_run(self, tracker):
        """The adapter documents this: a second start is a no-op."""
        first = tracker.start_run(run_name="live-once")
        assert tracker.start_run(run_name="live-twice") == first


class TestLogging:
    def test_params_land_in_the_store(self, tracker, client):
        run_id = tracker.start_run(run_name="live-params")
        tracker.log_param("n_estimators", 300)
        tracker.log_params({"max_depth": 6, "objective": "binary:logistic"})
        tracker.end_run(status=RunStatus.SUCCESS)

        params = client.get_run(run_id).data.params
        # MLflow stringifies every param value.
        assert params["n_estimators"] == "300"
        assert params["max_depth"] == "6"
        assert params["objective"] == "binary:logistic"

    def test_metrics_land_in_the_store(self, tracker, client):
        run_id = tracker.start_run(run_name="live-metrics")
        tracker.log_metric("f1", 0.91)
        tracker.log_metrics({"roc_auc": 0.97, "accuracy": 0.88})
        tracker.end_run(status=RunStatus.SUCCESS)

        metrics = client.get_run(run_id).data.metrics
        assert metrics["f1"] == pytest.approx(0.91)
        assert metrics["roc_auc"] == pytest.approx(0.97)
        assert metrics["accuracy"] == pytest.approx(0.88)

    def test_metric_steps_are_preserved(self, tracker, client):
        run_id = tracker.start_run(run_name="live-steps")
        for step, value in enumerate([0.5, 0.7, 0.9]):
            tracker.log_metric("train_loss", value, step=step)
        tracker.end_run(status=RunStatus.SUCCESS)

        history = client.get_metric_history(run_id, "train_loss")
        assert [m.step for m in history] == [0, 1, 2]
        assert [pytest.approx(m.value) for m in history] == [0.5, 0.7, 0.9]

    def test_artifact_is_uploaded(self, tracker, client, tmp_path):
        """Exercises the artifact store, not just the backend store.

        The deployed server runs without ``--serve-artifacts``, so this
        is a direct client-to-S3 upload — a path no fake can cover.
        """
        f = tmp_path / "metrics.json"
        f.write_text('{"f1": 0.91}')

        run_id = tracker.start_run(run_name="live-artifact")
        try:
            tracker.log_artifact(str(f))
        except Exception as exc:  # noqa: BLE001 - environment dependent
            pytest.skip(f"artifact store not writable from here: {exc}")
        tracker.end_run(status=RunStatus.SUCCESS)

        assert [a.path for a in client.list_artifacts(run_id)] == ["metrics.json"]

    def test_logging_without_an_active_run_raises(self, tracker):
        with pytest.raises(ExperimentTrackingError, match="No active MLflow run"):
            tracker.log_metric("f1", 0.5)


class TestStatusMappingIsAcceptedByMlflow:
    """The mapping must produce statuses the server actually stores.

    The unit tests assert ``_map_run_status`` returns "FINISHED" /
    "FAILED" / "KILLED"; only a real server confirms MLflow accepts
    those and reports them back unchanged.
    """

    @pytest.mark.parametrize(
        "framework_status,mlflow_status",
        [
            (RunStatus.SUCCESS, "FINISHED"),
            (RunStatus.FAILED, "FAILED"),
            (RunStatus.CANCELLED, "KILLED"),
        ],
    )
    def test_terminal_status_round_trips(
        self, tracker, client, framework_status, mlflow_status
    ):
        run_id = tracker.start_run(run_name=f"live-{framework_status.value}")
        tracker.end_run(status=framework_status)
        assert client.get_run(run_id).info.status == mlflow_status

    def test_end_run_without_a_run_is_a_no_op(self, tracker):
        tracker.end_run(status=RunStatus.SUCCESS)  # must not raise


class TestGlobalRunState:
    """MLflow's active run is process-global; the adapter's is per-instance.

    Two trackers in one process do not get independent runs — the
    second ``start_run`` nests or collides depending on MLflow's
    version. This test documents the real behaviour rather than
    asserting an isolation the adapter does not provide; see the
    module docstring of ``mlops_framework.tracking.mlflow``.
    """

    def test_second_tracker_does_not_get_an_independent_run(self, tracker):
        from mlops_framework.tracking.mlflow import MLflowTracker

        first_id = tracker.start_run(run_name="live-global-1")
        other = MLflowTracker(tracking_uri=TRACKING_URI, experiment_name=EXPERIMENT)

        try:
            second_id = other.start_run(run_name="live-global-2")
        except Exception:
            # MLflow refused the concurrent run outright — also proof
            # the two trackers share one global slot.
            return
        assert second_id != first_id
        assert mlflow.active_run() is not None
        # The nested run is what is active, so the first tracker's
        # subsequent logging would land on the wrong run.
        assert mlflow.active_run().info.run_id == second_id
        other.end_run(status=RunStatus.SUCCESS)
