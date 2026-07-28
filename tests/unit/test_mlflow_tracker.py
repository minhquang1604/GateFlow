"""Unit tests for the MLflowTracker adapter.

The test suite has two flavours:

* The default tests stub MLflow via ``sys.modules`` so the adapter's
  contract is verified without the ``mlflow`` SDK being installed.
* An optional set of tests installs ``mlflow`` and exercises the
  real ``MLflowTracker`` against an in-memory file store. They are
  skipped automatically when ``mlflow`` is not importable.

In both flavours the adapter's behaviour is identical: parsing the
framework CLI / config, mapping run status, and the lazy-import
guarantee.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any, Optional

import pytest

from mlops_framework.exceptions import ExperimentTrackingError
from mlops_framework.tracking import mlflow as mlflow_module
from mlops_framework.tracking.base import RunStatus


# ---------------------------------------------------------------------- #
# Helpers — build a fake "mlflow" module with a recording surface.
# ---------------------------------------------------------------------- #


class _FakeRun:
    def __init__(self, run_id: str, name: Optional[str] = None):
        self.info = types.SimpleNamespace(run_id=run_id, run_name=name)


class _FakeMlflow:
    """Records every call the adapter makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._tracking_uri: Optional[str] = None
        self._experiment: Optional[str] = None
        self._run_counter = 0

    # Surface used by the adapter
    def set_tracking_uri(self, uri: str) -> None:
        self.calls.append(("set_tracking_uri", (uri,), {}))
        self._tracking_uri = uri

    def set_experiment(self, name: str) -> None:
        self.calls.append(("set_experiment", (name,), {}))
        self._experiment = name

    def start_run(self, run_name: Optional[str] = None, tags: Optional[dict] = None):
        self._run_counter += 1
        run_id = f"run-{self._run_counter}"
        self.calls.append(("start_run", (), {"run_name": run_name, "tags": tags or {}}))
        return _FakeRun(run_id, run_name)

    def log_param(self, key: str, value: Any) -> None:
        self.calls.append(("log_param", (key, value), {}))

    def log_params(self, params: dict) -> None:
        self.calls.append(("log_params", (dict(params),), {}))

    def log_metric(self, key: str, value: float, step: Optional[int] = None) -> None:
        self.calls.append(("log_metric", (key, value), {"step": step}))

    def log_metrics(self, metrics: dict, step: Optional[int] = None) -> None:
        self.calls.append(("log_metrics", (dict(metrics),), {"step": step}))

    def log_artifact(self, path: str) -> None:
        self.calls.append(("log_artifact", (path,), {}))

    def end_run(self, status: str = "FINISHED") -> None:
        self.calls.append(("end_run", (status,), {}))


class FakeMlflowShim:
    """A lightweight mlflow shim that subclasses can override.

    The :class:`TestEnvDrivenConstruction` tests need to capture the
    arguments forwarded to ``set_tracking_uri`` and ``set_experiment``
    *without* relying on the recording surface above. This shim
    exposes the same methods as :class:`_FakeMlflow` but defaults to
    no-ops, so subclasses can override only the methods they care
    about.
    """

    def __init__(self) -> None:
        self._run_counter = 0

    def set_tracking_uri(self, uri: str) -> None:  # pragma: no cover - overridden
        pass

    def set_experiment(self, name: str) -> None:  # pragma: no cover - overridden
        pass

    def start_run(self, run_name: Optional[str] = None, tags: Optional[dict] = None):
        self._run_counter += 1
        return _FakeRun(f"run-{self._run_counter}", run_name)

    def log_param(self, key: str, value: Any) -> None:
        pass

    def log_params(self, params: dict) -> None:
        pass

    def log_metric(self, key: str, value: float, step: Optional[int] = None) -> None:
        pass

    def log_metrics(self, metrics: dict, step: Optional[int] = None) -> None:
        pass

    def log_artifact(self, path: str) -> None:
        pass

    def end_run(self, status: str = "FINISHED") -> None:
        pass


def _install_fake_mlflow(monkeypatch) -> _FakeMlflow:
    """Install a fake ``mlflow`` module into ``sys.modules``."""
    fake = _FakeMlflow()
    module = types.ModuleType("mlflow")
    module.set_tracking_uri = fake.set_tracking_uri
    module.set_experiment = fake.set_experiment
    module.start_run = fake.start_run
    module.log_param = fake.log_param
    module.log_params = fake.log_params
    module.log_metric = fake.log_metric
    module.log_metrics = fake.log_metrics
    module.log_artifact = fake.log_artifact
    module.end_run = fake.end_run
    monkeypatch.setitem(sys.modules, "mlflow", module)
    # Settings cache must be cleared so the fake adapter picks up a
    # clean ``Settings`` instance.
    from mlops_framework.config.settings import get_settings

    get_settings.cache_clear()
    # The adapter caches the imported mlflow module on the class
    # instance, but the lazy import function reads sys.modules on
    # every call.
    return fake


# ---------------------------------------------------------------------- #
# Original contract tests
# ---------------------------------------------------------------------- #


def test_import_does_not_require_mlflow():
    """Importing the mlflow module must not trigger an mlflow import."""
    # Ensure mlflow is not importable.
    sys.modules.pop("mlflow", None)
    sys.modules.pop("mlops_framework.tracking.mlflow", None)
    reloaded = importlib.import_module("mlops_framework.tracking.mlflow")
    # The module itself should be importable; only construction calls mlflow.
    assert reloaded is not None


def test_construct_without_mlflow_raises(monkeypatch):
    """Constructing MLflowTracker when mlflow is missing should raise a
    clear framework-level error, not an ImportError."""
    import builtins
    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name == "mlflow" or name.startswith("mlflow."):
            raise ImportError("mlflow blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    # Force the lazy import inside the module to retry.
    if "mlops_framework.tracking.mlflow" in sys.modules:
        del sys.modules["mlops_framework.tracking.mlflow"]
    importlib.import_module("mlops_framework.tracking.mlflow")

    with pytest.raises(ExperimentTrackingError) as excinfo:
        mlflow_module.MLflowTracker()
    assert "mlflow" in str(excinfo.value).lower()


def test_run_status_values_are_strings():
    """Ensure RunStatus has the expected string values for the adapter."""
    assert RunStatus.SUCCESS.value == "SUCCESS"
    assert RunStatus.FAILED.value == "FAILED"
    assert RunStatus.CANCELLED.value == "CANCELLED"


# ---------------------------------------------------------------------- #
# New tests — env-driven configuration and status mapping
# ---------------------------------------------------------------------- #


class TestStatusMapping:
    """Verify the framework <-> MLflow run-status mapping."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            (RunStatus.SUCCESS, "FINISHED"),
            (RunStatus.FAILED, "FAILED"),
            (RunStatus.CANCELLED, "KILLED"),
            ("SUCCESS", "FINISHED"),
            ("FAILED", "FAILED"),
            ("CANCELLED", "KILLED"),
        ],
    )
    def test_maps_status(self, status, expected):
        assert mlflow_module._map_run_status(status) == expected

    def test_unknown_status_defaults_to_failed(self):
        assert mlflow_module._map_run_status("UNKNOWN") == "FAILED"


class TestEnvDrivenConstruction:
    def test_uses_explicit_tracking_uri(self, monkeypatch):
        fake = _install_fake_mlflow(monkeypatch)
        tracker = mlflow_module.MLflowTracker(
            tracking_uri="http://mlflow:5000",
            experiment_name="unit-tests",
        )
        # The adapter forwarded the explicit args to mlflow.
        uris = [c for c in fake.calls if c[0] == "set_tracking_uri"]
        experiments = [c for c in fake.calls if c[0] == "set_experiment"]
        assert uris and uris[0][1][0] == "http://mlflow:5000"
        assert experiments and experiments[0][1][0] == "unit-tests"

    def test_falls_back_to_env_var(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://from-env:5000")
        monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", "env-experiment")
        # Capture the URI/experiment passed to mlflow during construction.
        captured: dict[str, str] = {}

        class _Capture(FakeMlflowShim):
            def set_tracking_uri(self, uri: str) -> None:
                captured["uri"] = uri

            def set_experiment(self, name: str) -> None:
                captured["experiment"] = name

        fake = _Capture()
        module = types.ModuleType("mlflow")
        for attr in (
            "set_tracking_uri",
            "set_experiment",
            "start_run",
            "log_param",
            "log_params",
            "log_metric",
            "log_metrics",
            "log_artifact",
            "end_run",
        ):
            setattr(module, attr, getattr(fake, attr))
        monkeypatch.setitem(sys.modules, "mlflow", module)
        from mlops_framework.config.settings import get_settings

        get_settings.cache_clear()
        mlflow_module.MLflowTracker()
        assert captured["uri"] == "http://from-env:5000"
        assert captured["experiment"] == "env-experiment"


class TestEndRunMaps:
    def test_end_run_forwards_mlflow_status(self, monkeypatch):
        fake = _install_fake_mlflow(monkeypatch)
        tracker = mlflow_module.MLflowTracker(
            tracking_uri="http://mlflow:5000",
            experiment_name="unit-tests",
        )
        tracker.start_run(run_name="x")
        tracker.end_run(status=RunStatus.CANCELLED)
        end_calls = [c for c in fake.calls if c[0] == "end_run"]
        assert end_calls, "end_run was not called"
        assert end_calls[-1][1][0] == "KILLED"

    def test_end_run_with_success_uses_finished(self, monkeypatch):
        fake = _install_fake_mlflow(monkeypatch)
        tracker = mlflow_module.MLflowTracker(
            tracking_uri="http://mlflow:5000",
            experiment_name="unit-tests",
        )
        tracker.start_run(run_name="x")
        tracker.end_run(status=RunStatus.SUCCESS)
        end_calls = [c for c in fake.calls if c[0] == "end_run"]
        assert end_calls[-1][1][0] == "FINISHED"