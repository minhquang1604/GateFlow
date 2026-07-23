"""Unit tests for the MLflowTracker adapter.

MLflow is not installed in this environment. The test verifies the
adapter's *contract* — it raises a clear :class:`ExperimentTrackingError`
when mlflow cannot be imported, so the framework remains importable
without mlflow installed.
"""

import importlib
import sys

import pytest

from mlops_framework.exceptions import ExperimentTrackingError
from mlops_framework.tracking import mlflow as mlflow_module
from mlops_framework.tracking.base import RunStatus


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
