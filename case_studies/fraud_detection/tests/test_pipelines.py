"""Tests for the fraud-detection case study's pipeline functions.

Covers the one failure mode README's "Known limitations" flagged as a
silent one: a real error while logging a completed run to MLflow (most
commonly a missing MLFLOW_S3_ENDPOINT_URL / AWS credential pair, which
makes the artifact upload fail with AccessDenied) used to be swallowed
by a bare ``except Exception: print(...)`` in train_xgboost(). Training
itself must still succeed; the failure must be visible on the returned
result instead of only in a worker's stdout.
"""

from __future__ import annotations

import mlflow

from case_studies.fraud_detection.data import write_csv
from case_studies.fraud_detection.pipelines import train_xgboost


def _tracker_run_id(tracking_uri: str) -> str:
    mlflow.set_tracking_uri(tracking_uri)
    # Explicit, not the default experiment: mlflow's fluent API caches the
    # active experiment id process-globally, and an id left over from an
    # experiment created against a *different* tracking store (a different
    # test's sqlite file) will not exist in this fresh one.
    mlflow.set_experiment("test-fraud-detection-pipeline")
    with mlflow.start_run() as run:
        run_id = run.info.run_id
    return run_id


class TestTrainXgboostMlflowLogging:
    def test_a_real_logging_failure_is_surfaced_not_swallowed(self, tmp_path, monkeypatch):
        csv_path = write_csv(tmp_path / "fraud.csv", n_rows=200, fraud_ratio=0.1, seed=1)
        tracking_uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
        tracker_run_id = _tracker_run_id(tracking_uri)

        def _boom(*args, **kwargs):
            raise OSError("AccessDenied: check MLFLOW_S3_ENDPOINT_URL / AWS credentials")

        monkeypatch.setattr(mlflow, "log_artifact", _boom)

        result = train_xgboost(
            {
                "csv_uri": str(csv_path),
                "n_estimators": 5,
                "max_depth": 2,
                "tracker_run_id": tracker_run_id,
                "tracking_uri": tracking_uri,
            }
        )

        # Training succeeded — this is not a training failure.
        assert result["status"] == "SUCCESS"
        assert "metrics" in result
        # ...but the logging failure is visible on the result, not just printed.
        assert "AccessDenied" in result["mlflow_logging_warning"]

    def test_mlflow_not_installed_is_still_a_silent_skip(self, tmp_path, monkeypatch):
        """The legitimate case — no mlflow package at all — stays silent."""
        csv_path = write_csv(tmp_path / "fraud.csv", n_rows=200, fraud_ratio=0.1, seed=1)

        import builtins

        real_import = builtins.__import__

        def _no_mlflow(name, *args, **kwargs):
            if name == "mlflow":
                raise ImportError("No module named 'mlflow'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_mlflow)

        result = train_xgboost(
            {
                "csv_uri": str(csv_path),
                "n_estimators": 5,
                "max_depth": 2,
                "tracker_run_id": "some-run-id",
            }
        )

        assert result["status"] == "SUCCESS"
        assert "mlflow_logging_warning" not in result
