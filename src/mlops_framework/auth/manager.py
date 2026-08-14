"""ApiKeyManager — mint, resolve and revoke scoped API keys.

Lives under its own top-level package rather than ``api/`` for the same
reason ``audit`` and ``tracking.mlflow_registry`` do: ``api/security.py``
needs it on every request, and a CLI or a migration script that mints a
first key must not have to import the whole FastAPI app to reach it.

Scopes
------
Three, and deliberately not more:

* ``read``   — every GET the console renders from.
* ``write``  — anything that changes state: promote, rollback, start a
  training run, create or fire a schedule, edit a policy.
* ``admin``  — manage keys themselves.

``admin`` implies ``write`` implies ``read`` (see :func:`effective_scopes`).
A role system with inheritance tables would be ceremony around three
values; if a fourth is ever needed, this is the file that grows.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.api_key import ApiKey
from mlops_framework.exceptions import ApiKeyError

# The visible prefix makes a leaked string identifiable as a credential
# for this framework — worth more than the four bytes it costs, because
# secret scanners key off exactly this.
KEY_PREFIX = "mlops_ak_"
PREFIX_SAMPLE_LENGTH = 6

SCOPE_READ = "read"
SCOPE_WRITE = "write"
SCOPE_ADMIN = "admin"
VALID_SCOPES = frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN})

# Who implies whom. Kept as data rather than a chain of ifs so
# effective_scopes() stays a lookup and the relationship is readable.
_IMPLIES: dict[str, set[str]] = {
    SCOPE_ADMIN: {SCOPE_ADMIN, SCOPE_WRITE, SCOPE_READ},
    SCOPE_WRITE: {SCOPE_WRITE, SCOPE_READ},
    SCOPE_READ: {SCOPE_READ},
}


def effective_scopes(scopes: set[str] | list[str]) -> set[str]:
    """Expand granted scopes to everything they imply."""
    out: set[str] = set()
    for scope in scopes:
        out |= _IMPLIES.get(scope, {scope})
    return out


def hash_key(plaintext: str) -> str:
    """sha256 of a key. Plain, not salted+stretched, on purpose.

    A password needs a slow KDF because it is low-entropy and chosen by
    a human. This is 256 bits from ``secrets.token_urlsafe``: there is
    no dictionary to run against it, and making the lookup slow would
    only tax every authenticated request. What matters here is that the
    stored form is one-way, and that lookup is by an indexed exact
    match.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    """Who the current request is acting as.

    ``name`` is what lands in ``AuditLog.actor``. Unlike the ``X-Actor``
    header it replaces, it is derived from a credential the caller had
    to possess, so an audit row is evidence rather than a claim.

    ``via_shared_secret`` marks the transitional path: a request
    authenticated with ``CONSOLE_WRITE_TOKEN`` rather than a key. Those
    requests still work and still get ``write``, but nothing verified
    who they are — see ``api/security.py``.
    """

    name: str
    scopes: frozenset[str]
    api_key_id: int | None = None
    via_shared_secret: bool = False

    def has(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True)
class MintedKey:
    """A newly created key. ``plaintext`` is available here and nowhere
    else, ever again — see the model docstring."""

    id: int
    name: str
    plaintext: str
    key_prefix: str
    scopes: list[str]


class ApiKeyManager:
    """Manages ApiKey entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        name: str,
        scopes: list[str],
        description: str | None = None,
    ) -> MintedKey:
        """Mint a key. The plaintext is returned once and not stored.

        Raises:
            ApiKeyError: unknown scope, empty scope list, or a name
                already in use.
        """
        unknown = sorted(set(scopes) - VALID_SCOPES)
        if unknown:
            raise ApiKeyError(
                f"unknown scope(s) {unknown}; valid scopes are "
                f"{sorted(VALID_SCOPES)}"
            )
        if not scopes:
            raise ApiKeyError("a key with no scopes could not do anything")
        if self.get_by_name(name) is not None:
            raise ApiKeyError(f"an API key named {name!r} already exists")

        plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
        row = ApiKey(
            name=name,
            key_hash=hash_key(plaintext),
            key_prefix=plaintext[: len(KEY_PREFIX) + PREFIX_SAMPLE_LENGTH],
            scopes_json=json.dumps(sorted(set(scopes))),
            description=description,
        )
        self._session.add(row)
        self._session.flush()
        return MintedKey(
            id=row.id,
            name=row.name,
            plaintext=plaintext,
            key_prefix=row.key_prefix,
            scopes=sorted(set(scopes)),
        )

    def get_by_name(self, name: str) -> ApiKey | None:
        return self._session.execute(
            select(ApiKey).where(ApiKey.name == name)
        ).scalars().first()

    def list_keys(self, *, include_revoked: bool = False) -> list[ApiKey]:
        stmt = select(ApiKey).order_by(ApiKey.id)
        if not include_revoked:
            stmt = stmt.where(ApiKey.revoked_at.is_(None))
        return list(self._session.execute(stmt).scalars().all())

    def resolve(self, plaintext: str) -> Principal | None:
        """Turn a presented key into a Principal, or ``None``.

        ``None`` covers every failure — unknown key, revoked key,
        malformed input — on purpose: telling a caller *which* of those
        it was would confirm that a given string is a real key.

        The hash comparison uses ``compare_digest`` even though the
        lookup is already an indexed equality match. The index makes
        that comparison the only place a timing difference could show
        up, and it costs nothing to close.
        """
        if not plaintext:
            return None
        digest = hash_key(plaintext)
        row = self._session.execute(
            select(ApiKey).where(ApiKey.key_hash == digest)
        ).scalars().first()
        if row is None or not hmac.compare_digest(row.key_hash, digest):
            return None
        if row.revoked_at is not None:
            return None

        row.last_used_at = datetime.now(UTC)
        try:
            scopes = set(json.loads(row.scopes_json or "[]"))
        except (TypeError, ValueError):
            scopes = set()
        return Principal(
            name=row.name,
            scopes=frozenset(effective_scopes(scopes)),
            api_key_id=row.id,
        )

    def revoke(self, name: str) -> ApiKey:
        """Revoke by name. Idempotent — re-revoking is not an error, and
        the original timestamp is kept.

        Raises:
            ApiKeyError: no such key.
        """
        row = self.get_by_name(name)
        if row is None:
            raise ApiKeyError(f"no API key named {name!r}")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            self._session.flush()
        return row
