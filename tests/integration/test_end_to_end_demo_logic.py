"""Hermetic tests for ``scripts.run_end_to_end_demo`` and the shared
``scripts._initial_training`` module it (and ``run_drift_recovery_demo``'s
Phase 1) is built on.

The full end-to-end demo exercises real MLflow, Airflow, and a
ServingBridge. These tests verify the *logic* of the demo script — the
service URLs it builds, the framework APIs it calls, the events it
publishes — without requiring any of the docker-compose services to be
running. They substitute a fake Airflow HTTP client and a
``LocalDockerOrchestrator`` + ``InMemoryTracker`` so the script can be
run/exercised end-to-end on a developer machine.

The tests are organised by "step" of the demo so failures point clearly
at the section responsible. Note that the endpoint-resolution,
service-wait, and Airflow/MLflow wiring all now live in
``scripts._initial_training`` (shared with the drift-recovery demo), not
in ``run_end_to_end_demo`` itself — see that module's docstring.
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

# ``run_end_to_end_demo`` imports the shared training flow as
# ``scripts._initial_training`` (it adds ``ROOT`` — not ``ROOT/scripts``
# — to sys.path, as a side effect of the import above). Importing it
# under the *same* dotted name here, rather than a bare
# ``import _initial_training``, is required for `monkeypatch.setattr`
# below to land on the module object `demo.main()` actually calls into
# — a bare import would resolve to a second, distinct module object for
# the same file, and patches on it would silently never be seen.
import scripts._initial_training as core  # noqa: E402


# ---------------------------------------------------------------------- #
# 1. dataset / version wiring
# ---------------------------------------------------------------------- #


class TestEndpointResolution:
    """``resolve_endpoints`` honors explicit env vars and falls back to
    the settings instance.
    """

    def test_resolve_endpoints_prefers_env(self, monkeypatch):
        from mlops_framework.config.settings import Settings

        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://env-mlflow:5000")
        monkeypatch.setenv("AIRFLOW_BASE_URL", "http://env-airflow:8080")
        monkeypatch.setenv("SERVING_BRIDGE_URL", "http://env-serving:8001")
        s = Settings()
        ep = core.resolve_endpoints(s)
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
        ep = core.resolve_endpoints(s)
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
        ep = core.resolve_endpoints(s)
        assert ep["mlflow_uri"].startswith("http://localhost")
        assert ep["airflow_url"].startswith("http://localhost")
        assert ep["serving_url"].startswith("http://localhost")

    def test_resolve_endpoints_includes_in_network_mlflow_uri(self, monkeypatch):
        """The Airflow `train` task runs inside the docker network, so it
        needs the in-network MLflow URI — separate from whatever URL this
        process itself uses to reach MLflow. See resolve_endpoints's
        mlflow_uri_for_airflow comment."""
        from mlops_framework.config.settings import Settings

        monkeypatch.delenv("AIRFLOW_INTERNAL_MLFLOW_URI", raising=False)
        ep = core.resolve_endpoints(Settings())
        assert ep["mlflow_uri_for_airflow"] == "http://mlflow:5000"


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

        monkeypatch.setattr(core.httpx, "get", fake_get)
        core._wait_for("http://localhost:9999", label="unit-test", timeout=5.0)
        assert calls["n"] >= 1

    def test_raises_on_timeout(self, monkeypatch):
        def fake_get(url, timeout=2.0):
            raise httpx.ConnectError("no connection")

        monkeypatch.setattr(core.httpx, "get", fake_get)
        with pytest.raises(RuntimeError) as excinfo:
            core._wait_for(
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
        the adapters (on ``_initial_training``, where they are actually
        used — ``run_end_to_end_demo`` no longer imports them directly)
        so the script doesn't actually hit Airflow/MLflow.
        """
        import mlops_framework.tracking.in_memory as in_memory
        import mlops_framework.orchestration.local as local

        # Force the script to use the local orchestrator + in-memory
        # tracker so we don't need a live MLflow/Airflow deployment.
        # InMemoryTracker() takes no arguments (see tests elsewhere that
        # use it directly) while run_initial_training always calls
        # MLflowTracker(tracking_uri=..., experiment_name=...) — wrap it
        # so the substitution is a proper drop-in.
        monkeypatch.setattr(
            core,
            "MLflowTracker",
            lambda *args, **kwargs: in_memory.InMemoryTracker(),
        )
        # Same story: LocalDockerOrchestrator() takes no constructor
        # kwargs, but run_initial_training always calls
        # AirflowOrchestrator(base_url=..., username=..., password=...).
        monkeypatch.setattr(
            core,
            "AirflowOrchestrator",
            lambda *args, **kwargs: local.LocalDockerOrchestrator(),
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
        # A short --timeout: LocalDockerOrchestrator really runs the
        # pipeline locally against the (unreachable) "nowhere" MLflow
        # URL, so the demo's SUCCESS-wait loop would otherwise poll for
        # the full 600s default before giving up.
        monkeypatch.setattr(
            sys, "argv", ["run_end_to_end_demo.py", "--skip-wait", "--timeout", "10"]
        )
        try:
            demo.main()  # type: ignore[func-returns-value]
        except SystemExit as exc:
            # run_initial_training raises SystemExit(<reason string>)
            # on a blocking failure (readiness/DAG/promotion), so
            # ``.code`` here is a descriptive message, not a small int
            # — the important thing is that it's a *controlled* failure
            # (we never actually ran Airflow) and not some unrelated
            # crash during the wiring phase, which would raise a
            # different exception type and fail this test outright.
            assert exc.code not in (None, 0)
        finally:
            get_settings.cache_clear()
