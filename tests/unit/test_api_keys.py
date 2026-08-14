"""``ApiKeyManager`` — minting, resolving, scoping and revoking.

The framework's first per-principal identity. What matters most here is
what the manager *refuses* and what it never stores: a key row that
could be turned back into a credential would defeat the point.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mlops_framework.auth.manager import (
    KEY_PREFIX,
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKeyManager,
    effective_scopes,
    hash_key,
)
from mlops_framework.database.base import Base
from mlops_framework.database.models import ApiKey  # noqa: F401 - registers the table
from mlops_framework.exceptions import ApiKeyError


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


class TestScopeImplication:
    def test_admin_implies_write_and_read(self):
        assert effective_scopes([SCOPE_ADMIN]) == {SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ}

    def test_write_implies_read(self):
        assert effective_scopes([SCOPE_WRITE]) == {SCOPE_WRITE, SCOPE_READ}

    def test_read_implies_only_itself(self):
        assert effective_scopes([SCOPE_READ]) == {SCOPE_READ}


class TestMinting:
    def test_returns_a_prefixed_plaintext_once(self, session):
        minted = ApiKeyManager(session).create(name="alice", scopes=["write"])
        assert minted.plaintext.startswith(KEY_PREFIX)
        assert len(minted.plaintext) > len(KEY_PREFIX) + 20

    def test_the_plaintext_is_never_stored(self, session):
        mgr = ApiKeyManager(session)
        minted = mgr.create(name="alice", scopes=["write"])
        session.commit()

        row = mgr.get_by_name("alice")
        assert row.key_hash == hash_key(minted.plaintext)
        assert minted.plaintext not in row.key_hash
        # Nothing on the row is the key, and nothing on it can rebuild it.
        stored = json.dumps(
            {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
        )
        assert minted.plaintext not in stored

    def test_the_prefix_identifies_without_authenticating(self, session):
        mgr = ApiKeyManager(session)
        minted = mgr.create(name="alice", scopes=["read"])
        session.commit()
        row = mgr.get_by_name("alice")

        assert minted.plaintext.startswith(row.key_prefix)
        # …and the prefix alone is not a credential.
        assert mgr.resolve(row.key_prefix) is None

    def test_two_keys_are_never_the_same(self, session):
        mgr = ApiKeyManager(session)
        a = mgr.create(name="a", scopes=["read"])
        b = mgr.create(name="b", scopes=["read"])
        assert a.plaintext != b.plaintext

    def test_scopes_are_stored_sorted_and_deduplicated(self, session):
        mgr = ApiKeyManager(session)
        mgr.create(name="alice", scopes=["write", "read", "write"])
        session.commit()
        assert json.loads(mgr.get_by_name("alice").scopes_json) == ["read", "write"]


class TestMintingRefusals:
    def test_unknown_scope(self, session):
        with pytest.raises(ApiKeyError, match="unknown scope"):
            ApiKeyManager(session).create(name="a", scopes=["superuser"])

    def test_no_scopes(self, session):
        with pytest.raises(ApiKeyError, match="could not do anything"):
            ApiKeyManager(session).create(name="a", scopes=[])

    def test_duplicate_name(self, session):
        mgr = ApiKeyManager(session)
        mgr.create(name="alice", scopes=["read"])
        session.commit()
        with pytest.raises(ApiKeyError, match="already exists"):
            mgr.create(name="alice", scopes=["read"])


class TestResolving:
    def test_resolves_to_a_named_principal(self, session):
        mgr = ApiKeyManager(session)
        minted = mgr.create(name="alice", scopes=["write"])
        session.commit()

        principal = mgr.resolve(minted.plaintext)
        assert principal is not None
        assert principal.name == "alice"
        assert principal.has(SCOPE_WRITE)
        assert principal.has(SCOPE_READ)  # implied
        assert not principal.has(SCOPE_ADMIN)
        assert principal.via_shared_secret is False

    def test_records_last_used(self, session):
        mgr = ApiKeyManager(session)
        minted = mgr.create(name="alice", scopes=["read"])
        session.commit()
        assert mgr.get_by_name("alice").last_used_at is None

        mgr.resolve(minted.plaintext)
        session.commit()
        assert mgr.get_by_name("alice").last_used_at is not None

    @pytest.mark.parametrize(
        "presented", ["", "nonsense", KEY_PREFIX + "wrong", "Bearer something"]
    )
    def test_anything_that_is_not_a_key_resolves_to_nobody(self, session, presented):
        ApiKeyManager(session).create(name="alice", scopes=["read"])
        session.commit()
        assert ApiKeyManager(session).resolve(presented) is None

    def test_a_revoked_key_resolves_to_nobody(self, session):
        mgr = ApiKeyManager(session)
        minted = mgr.create(name="leaked", scopes=["admin"])
        session.commit()
        assert mgr.resolve(minted.plaintext) is not None

        mgr.revoke("leaked")
        session.commit()
        assert mgr.resolve(minted.plaintext) is None


class TestRevoking:
    def test_sets_a_timestamp_rather_than_deleting(self, session):
        """A key that acted has to stay resolvable for as long as the
        audit rows naming it do."""
        mgr = ApiKeyManager(session)
        mgr.create(name="alice", scopes=["read"])
        session.commit()

        mgr.revoke("alice")
        session.commit()

        row = mgr.get_by_name("alice")
        assert row is not None
        assert row.revoked_at is not None

    def test_is_idempotent_and_keeps_the_first_timestamp(self, session):
        mgr = ApiKeyManager(session)
        mgr.create(name="alice", scopes=["read"])
        session.commit()
        first = mgr.revoke("alice").revoked_at
        session.commit()
        again = mgr.revoke("alice").revoked_at
        # Compared naive: SQLite has no tz-aware storage, so the value
        # comes back without tzinfo after a round trip while the
        # in-memory one still carries UTC. Same instant either way, and
        # Postgres (timestamptz) keeps the offset.
        assert again.replace(tzinfo=None) == first.replace(tzinfo=None)

    def test_unknown_name(self, session):
        with pytest.raises(ApiKeyError, match="no API key"):
            ApiKeyManager(session).revoke("nobody")

    def test_revoked_keys_are_hidden_from_the_default_listing(self, session):
        mgr = ApiKeyManager(session)
        mgr.create(name="live", scopes=["read"])
        mgr.create(name="dead", scopes=["read"])
        session.commit()
        mgr.revoke("dead")
        session.commit()

        assert [k.name for k in mgr.list_keys()] == ["live"]
        assert {k.name for k in mgr.list_keys(include_revoked=True)} == {"live", "dead"}
