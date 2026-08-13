"""Unit tests for ``mlops_framework.api.security.require_write_token``."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from mlops_framework.api.security import require_write_token
from mlops_framework.config.settings import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestNoTokenConfigured:
    def test_unset_rejects_even_with_a_header(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_WRITE_TOKEN", raising=False)
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            require_write_token(x_console_token="anything")
        assert exc_info.value.status_code == 503

    def test_unset_rejects_with_no_header(self, monkeypatch):
        monkeypatch.delenv("CONSOLE_WRITE_TOKEN", raising=False)
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            require_write_token(x_console_token=None)
        assert exc_info.value.status_code == 503


class TestTokenConfigured:
    def test_matching_token_passes(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()
        assert require_write_token(x_console_token="secret-123") is None

    def test_missing_header_is_401(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            require_write_token(x_console_token=None)
        assert exc_info.value.status_code == 401

    def test_wrong_token_is_403(self, monkeypatch):
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            require_write_token(x_console_token="wrong")
        assert exc_info.value.status_code == 403
