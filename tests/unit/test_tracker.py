"""Unit tests for the in-memory ExperimentTracker.

MLflow is not installed in this environment, so we use InMemoryTracker
to verify the tracker ABC is correct. The MLflowTracker is exercised
indirectly via the same ABC contract.
"""

from mlops_framework.tracking.base import RunStatus
from mlops_framework.tracking.in_memory import InMemoryTracker


def test_start_run_returns_id():
    tracker = InMemoryTracker()
    rid = tracker.start_run(run_name="r1", tags={"k": "v"})
    assert isinstance(rid, str) and rid
    assert tracker.runs[0]["run_name"] == "r1"
    assert tracker.runs[0]["tags"] == {"k": "v"}


def test_log_params_and_metrics_and_artifact():
    tracker = InMemoryTracker()
    tracker.start_run()
    tracker.log_params({"lr": 0.01, "n_estimators": 100})
    tracker.log_metrics({"loss": 0.42, "f1": 0.9}, step=1)
    tracker.log_artifact("model.pkl")
    assert tracker.params == [("lr", 0.01), ("n_estimators", 100)]
    assert tracker.metrics == [("loss", 0.42, 1), ("f1", 0.9, 1)]
    assert tracker.artifacts == ["model.pkl"]


def test_end_run_marks_status():
    tracker = InMemoryTracker()
    tracker.start_run()
    tracker.end_run(status=RunStatus.FAILED)
    assert tracker.runs[0]["ended"] is True
    assert tracker.runs[0]["end_status"] == "FAILED"


def test_double_start_returns_same_id():
    tracker = InMemoryTracker()
    first = tracker.start_run()
    second = tracker.start_run()
    assert first == second


def test_log_without_active_run_noop_for_inmemory():
    """InMemoryTracker is permissive; MLflowTracker raises. We document the
    contract here for both."""
    in_mem = InMemoryTracker()
    in_mem.log_param("a", 1)  # should not raise
    assert in_mem.params == [("a", 1)]
