"""Unit tests for ``mlops_framework.api.security``.

This file used to call ``require_write_token(x_console_token=...)``
directly, back when the gate was one shared-secret comparison and that
was its whole signature. Authorization is now scope-based over a
:class:`~mlops_framework.auth.manager.Principal`, so the pieces worth
isolating here are different: how a credential is parsed, and which of
the three refusals each situation produces.

The end-to-end behaviour over real routes lives in
``tests/api/test_write_auth.py``.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.api.security import (
    ANONYMOUS_ACTOR,
    _bearer_token,
    get_actor,
    get_principal,
    require_admin,
    require_write,
)
from mlops_framework.auth.manager import ApiKeyManager, Principal
from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base
from mlops_framework.database.models import ApiKey  # noqa: F401 - registers the table


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


class TestBearerParsing:
    @pytest.mark.parametrize(
        "header,expected",
        [
            ("Bearer mlops_ak_abc", "mlops_ak_abc"),
            ("Bearer   spaced  ", "spaced"),
            (None, None),
            ("", None),
            ("Bearer", None),
            ("Bearer ", None),
            # Not our scheme — a Basic credential must not be read as a key.
            ("Basic dXNlcjpwYXNz", None),
            ("mlops_ak_abc", None),  # no scheme at all
        ],
    )
    def test_parses_only_a_bearer_scheme(self, header, expected):
        assert _bearer_token(header) == expected


class TestResolution:
    def test_a_valid_key_resolves(self, session, monkeypatch):
        monkeypatch.delenv("CONSOLE_WRITE_TOKEN", raising=False)
        get_settings.cache_clear()
        key = ApiKeyManager(session).create(name="alice", scopes=["write"]).plaintext

        principal = get_principal(
            authorization=f"Bearer {key}", x_console_token=None, x_actor=None, db=session
        )
        assert principal is not None
        assert principal.name == "alice"
        assert principal.via_shared_secret is False

    def test_the_shared_secret_resolves_with_write(self, session, monkeypatch):
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()

        principal = get_principal(
            authorization=None, x_console_token="secret-123", x_actor="alice", db=session
        )
        assert principal is not None
        assert principal.has("write")
        assert not principal.has("admin")
        # Marked, because nothing verified that this really is alice.
        assert principal.via_shared_secret is True
        assert principal.name == "alice"

    def test_the_shared_secret_without_an_actor_is_system(self, session, monkeypatch):
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()
        principal = get_principal(
            authorization=None, x_console_token="secret-123", x_actor=None, db=session
        )
        assert principal.name == ANONYMOUS_ACTOR

    def test_a_bad_key_is_not_downgraded_to_the_shared_secret(self, session, monkeypatch):
        """Presenting a key means intending to use it. Quietly succeeding
        as someone else would put the wrong name in the audit trail."""
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()

        assert (
            get_principal(
                authorization="Bearer mlops_ak_wrong",
                x_console_token="secret-123",
                x_actor=None,
                db=session,
            )
            is None
        )

    def test_nothing_presented_resolves_to_nobody(self, session, monkeypatch):
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()
        assert (
            get_principal(authorization=None, x_console_token=None, x_actor=None, db=session)
            is None
        )


class TestRefusals:
    def test_nothing_configured_and_no_keys_is_503(self, session, monkeypatch):
        """The deployment cannot authenticate anyone — saying so beats a
        401 that no credential could satisfy."""
        monkeypatch.delenv("CONSOLE_WRITE_TOKEN", raising=False)
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            require_write(principal=None, db=session)
        assert exc_info.value.status_code == 503

    def test_configured_but_nothing_presented_is_401(self, session, monkeypatch):
        monkeypatch.setenv("CONSOLE_WRITE_TOKEN", "secret-123")
        get_settings.cache_clear()
        with pytest.raises(HTTPException) as exc_info:
            require_write(principal=None, db=session)
        assert exc_info.value.status_code == 401

    def test_a_key_existing_is_enough_to_make_it_401(self, session, monkeypatch):
        """Even with no shared secret: keys exist, so a caller *could*
        authenticate, and 401 is the accurate answer."""
        monkeypatch.delenv("CONSOLE_WRITE_TOKEN", raising=False)
        get_settings.cache_clear()
        ApiKeyManager(session).create(name="alice", scopes=["read"])
        session.flush()

        with pytest.raises(HTTPException) as exc_info:
            require_write(principal=None, db=session)
        assert exc_info.value.status_code == 401

    def test_known_caller_without_the_scope_is_403(self, session):
        reader = Principal(name="grafana", scopes=frozenset({"read"}))
        with pytest.raises(HTTPException) as exc_info:
            require_write(principal=reader, db=session)
        assert exc_info.value.status_code == 403
        assert "grafana" in exc_info.value.detail

    def test_write_is_not_admin(self, session):
        writer = Principal(name="alice", scopes=frozenset({"write", "read"}))
        with pytest.raises(HTTPException) as exc_info:
            require_admin(principal=writer, db=session)
        assert exc_info.value.status_code == 403

    def test_a_sufficient_scope_passes_and_returns_the_principal(self, session):
        writer = Principal(name="alice", scopes=frozenset({"write", "read"}))
        assert require_write(principal=writer, db=session) is writer


class TestActor:
    def test_comes_from_the_principal(self):
        assert get_actor(Principal(name="alice", scopes=frozenset({"write"}))) == "alice"

    def test_falls_back_to_system(self):
        assert get_actor(None) == ANONYMOUS_ACTOR
