"""Unit tests for ``mlops_framework.config.settings``."""

from __future__ import annotations

import pytest

from mlops_framework.config.settings import Settings, get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """Each test sees a fresh ``Settings`` instance."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestSettings:
    def test_default_database_url_is_postgres(self):
        s = Settings()
        assert "postgresql" in s.database_url
        assert "5432" in s.database_url

    def test_database_pool_defaults(self):
        s = Settings()
        assert s.database_pool_size == 5
        assert s.database_max_overflow == 10
        assert s.database_pool_timeout == 30
        assert s.database_echo is False

    def test_mlflow_defaults_are_empty_strings_or_none(self):
        s = Settings()
        # Either None or a string; we just need them to be defined.
        assert s.mlflow_tracking_uri is None or isinstance(s.mlflow_tracking_uri, str)
        assert isinstance(s.mlflow_experiment_name, str)
        assert s.mlflow_s3_endpoint_url is None or isinstance(s.mlflow_s3_endpoint_url, str)

    def test_airflow_defaults(self):
        s = Settings()
        assert s.airflow_username == "airflow"
        assert s.airflow_password == "airflow"
        assert s.airflow_base_url is None or isinstance(s.airflow_base_url, str)

    def test_serving_bridge_default(self):
        s = Settings()
        assert s.serving_bridge_url is None or isinstance(s.serving_bridge_url, str)

    def test_env_var_overrides(self, monkeypatch):
        monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://env-mlflow:5000")
        monkeypatch.setenv("MLFLOW_EXPERIMENT_NAME", "env-experiment")
        monkeypatch.setenv("AIRFLOW_BASE_URL", "http://env-airflow:8080")
        monkeypatch.setenv("SERVING_BRIDGE_URL", "http://env-serving:8001")
        s = Settings()
        assert s.mlflow_tracking_uri == "http://env-mlflow:5000"
        assert s.mlflow_experiment_name == "env-experiment"
        assert s.airflow_base_url == "http://env-airflow:8080"
        assert s.serving_bridge_url == "http://env-serving:8001"

    def test_app_metadata(self):
        s = Settings()
        assert s.app_name == "mlops-framework"
        assert s.app_version == "0.1.0"
        assert s.debug is False

    def test_get_settings_returns_instance(self):
        s1 = get_settings()
        s2 = get_settings()
        # Cached: same instance.
        assert s1 is s2