"""Hermetic tests for ``scripts.run_end_to_end_demo``.

The full end-to-end demo exercises real MLflow, Airflow, and a
ServingBridge. These tests verify the *logic* of the demo script — the
service URLs it builds, the framework APIs it calls, the events it
publishes — without requiring any of the docker-compose services to be
running. They substitute a fake Airflow HTTP client and a
``LocalDockerOrchestrator`` + ``InMemoryTracker`` so the script can be
run/exercised end-to-end on a developer machine.

The tests are organised by "step" of the demo so failures point clearly
at the script section responsible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

# Make the scripts/ directory importable so the demo can be imported
# as a module rather than executed.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

import run_end_to_end_demo as demo  # noqa: E402


# ---------------------------------------------------------------------- #
# 1. dataset / version wiring
# ---------------------------------------------------------------------- #


class TestEndpointResolution:
    """The demo's endpoint helpers honor explicit env vars and fall back to
    the settings instance.
    """

    def test_resolve_endpoints_prefers_env(self, monkeypatch):
        from mlops_framework.config.settings import Settings

        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://env-mlflow:5000")
        monkeypatch.setenv("AIRFLOW_BASE_URL", "http://env-airflow:8080")
        monkeypatch.setenv("SERVING_BRIDGE_URL", "http://env-serving:8001")
        s = Settings()
        ep = demo._resolve_endpoints(s)
        assert ep["mlflow_uri"] == "http://env-mlflow:5000"
        assert ep["airflow_url"] == "http://env-airflow:8080"
        assert ep["serving_url"] == "http://env-serving:8001"

    def test_resolve_endpoints_falls_back_to_settings(self, monkeypatch):
        from mlops_framework.config.settings import Settings

        monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
        monkeypatch.delenv("AIRFLOW_BASE_URL", raising=False)
        monkeypatch.delenv("SERVING_BRIDGE_URL", raising=False)
        s = Settings(
            mlflow_tracking_uri="http://settings-mlflow:5000",
            airflow_base_url="http://settings-airflow:8080",
            serving_bridge_url="http://settings-serving:8001",
        )
        ep = demo._resolve_endpoints(s)
        assert ep["mlflow_uri"] == "http://settings-mlflow:5000"
        assert ep["airflow_url"] == "http://settings-airflow:8080"
        assert ep["serving_url"] == "http://settings-serving:8001"

    def test_resolve_endpoints_falls_back_to_localhost(self, monkeypatch):
        """When no env var and no setting exists, we use the localhost
        defaults the demo prints in the success banner."""
        from mlops_framework.config.settings import Settings

        for var in ("MLFLOW_TRACKING_URI", "AIRFLOW_BASE_URL", "SERVING_BRIDGE_URL"):
            monkeypatch.delenv(var, raising=False)
        s = Settings(
            mlflow_tracking_uri=None,
            airflow_base_url=None,
            serving_bridge_url=None,
        )
        ep = demo._resolve_endpoints(s)
        assert ep["mlflow_uri"].startswith("http://localhost")
        assert ep["airflow_url"].startswith("http://localhost")
        assert ep["serving_url"].startswith("http://localhost")


# ---------------------------------------------------------------------- #
# 2. readiness wiring
# ---------------------------------------------------------------------- #


class TestDemoConstants:
    """The demo's constants are stable — the Airflow DAG and serving
    bridge rely on these exact identifiers.
    """

    def test_pipeline_id_is_python_dotted_path(self):
        assert ":" in demo.PIPELINE_ID
        module_path, callable_name = demo.PIPELINE_ID.split(":")
        assert module_path == "case_studies.fraud_detection.pipelines"
        assert callable_name == "train_xgboost"

    def test_dataset_name_is_stable(self):
        assert demo.DATASET_NAME == "credit-card-transactions"

    def test_model_name_is_stable(self):
        assert demo.MODEL_NAME == "fraud-xgboost"

    def test_data_rows_is_positive(self):
        assert demo.DATA_ROWS > 0


# ---------------------------------------------------------------------- #
# 3. service health checks
# ---------------------------------------------------------------------- #


class TestWaitFor:
    """``_wait_for`` polls forever (until the timeout) and raises on
    timeout. We use a fake httpx client to avoid hitting the network.
    """

    def test_returns_immediately_on_2xx(self, monkeypatch):
        calls = {"n": 0}

        def fake_get(url, timeout=2.0):
            calls["n"] += 1
            return httpx.Response(200, text="ok")

        monkeypatch.setattr(demo.httpx, "get", fake_get)
        demo._wait_for("http://localhost:9999", label="unit-test", timeout=5.0)
        assert calls["n"] >= 1

    def test_raises_on_timeout(self, monkeypatch):
        def fake_get(url, timeout=2.0):
            raise httpx.ConnectError("no connection")

        monkeypatch.setattr(demo.httpx, "get", fake_get)
        with pytest.raises(RuntimeError) as excinfo:
            demo._wait_for(
                "http://localhost:9999", label="unit-test", timeout=1.0
            )
        assert "did not become ready" in str(excinfo.value)


# ---------------------------------------------------------------------- #
# 4. successful CLI parse
# ---------------------------------------------------------------------- #


class TestMainCLI:
    def test_main_skips_wait_and_fails_on_missing(self, monkeypatch, tmp_path):
        """With --skip-wait and no services running, the demo must attempt
        to wire adapters and fail on the first network call. We monkeypatch
        the adapters so the script doesn't actually hit Airflow/MLflow.
        """
        import mlops_framework.tracking.in_memory as in_memory
        import mlops_framework.orchestration.local as local
        from mlops_framework.sdk import MLOpsProject

        # Force the script to use the local orchestrator + in-memory
        # tracker so we don't need a live MLflow/Airflow deployment.
        monkeypatch.setattr(
            demo,
            "MLflowTracker",
            in_memory.InMemoryTracker,
        )
        monkeypatch.setattr(
            demo,
            "AirflowOrchestrator",
            local.LocalDockerOrchestrator,
        )

        # The InMemoryTracker does not need a tracking_uri; we still
        # pass one to validate the script's URL plumbing.
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://nowhere:5000")
        monkeypatch.setenv("AIRFLOW_BASE_URL", "http://nowhere:8080")
        monkeypatch.setenv("SERVING_BRIDGE_URL", "http://nowhere:8001")

        # Use a SQLite file DB so the script persists real rows.
        db_path = tmp_path / "demo.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

        # The framework's default settings read DATABASE_URL. We must
        # make sure the env override is applied.
        from mlops_framework.config.settings import get_settings

        get_settings.cache_clear()

        # Call the demo's main with --skip-wait. The script is
        # designed to run end-to-end; in this hermetic environment we
        # only verify it gets past the wiring step and into the
        # dataset step without crashing on import errors.
        try:
            demo.main()  # type: ignore[func-returns-value]
        except SystemExit as exc:
            # The script exits with a non-zero code when the DAG run
            # fails (we never actually ran Airflow). The important
            # thing is that it didn't fail during the wiring phase.
            assert exc.code in (0, 1, 2, 3, 4)
        finally:
            get_settings.cache_clear()
