"""Authentication and authorization for the HTTP API.

Two credentials are accepted, and the difference between them is the
whole point of this module.

A **scoped API key** (``Authorization: Bearer mlops_ak_…``) resolves to a
named :class:`~mlops_framework.auth.manager.Principal` with a scope set.
The name is what lands in ``AuditLog.actor``, derived from a credential
the caller had to possess — so an audit row is evidence rather than a
claim, and two authorized operators are finally distinguishable.

The **shared secret** (``X-Console-Token``, ``CONSOLE_WRITE_TOKEN``) is
still accepted, and grants ``write``. It is the transitional path: it is
what closed the anonymous-write hole, it is what the deployed Airflow
DAG and docker-compose are configured with today, and removing it in the
same change that introduced keys would break every existing deployment
the moment it was pulled. A request authenticated this way is marked
``via_shared_secret`` and its actor falls back to the *unverified*
``X-Actor`` header, exactly as before — no better, but no worse, and
visibly so.

Scopes
------
``read`` < ``write`` < ``admin``; each implies the ones before it (see
``auth/manager.py``). Routes ask for what they need:

* nothing — the read surface the console renders from. Unauthenticated
  reads remain deliberate: the console has no login, and gating GETs
  would make it unusable without solving session management first.
* ``require_write`` — everything that changes state.
* ``require_admin`` — managing keys themselves.

Refusals
--------
* 503 when neither credential type is configured *and* no keys exist —
  the deployment cannot authenticate anyone, and saying so beats a 401
  that no credential could satisfy.
* 401 when nothing was presented, or what was presented did not resolve.
  A bad key and an unknown key are the same answer on purpose: telling
  them apart confirms that a given string is a real key.
* 403 when the caller is known but lacks the scope. Distinct from 401
  because "log in" and "you may not do this" are different fixes.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_db
from mlops_framework.auth.manager import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKeyManager,
    Principal,
)
from mlops_framework.config.settings import get_settings
from mlops_framework.database.models.api_key import ApiKey

HEADER_NAME = "X-Console-Token"
BEARER_PREFIX = "Bearer "

# The name recorded for a shared-secret request that sends no X-Actor.
# Same default get_actor has always used.
ANONYMOUS_ACTOR = "system"


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith(BEARER_PREFIX):
        return None
    return authorization[len(BEARER_PREFIX):].strip() or None


def get_principal(
    authorization: str | None = Header(default=None),
    x_console_token: str | None = Header(default=None, alias=HEADER_NAME),
    x_actor: str | None = Header(default=None, alias="X-Actor"),
    db: Session = Depends(get_db),
) -> Principal | None:
    """Resolve the caller, or ``None`` if they presented nothing valid.

    Returns rather than raises: read routes take this to *record* who is
    calling without requiring it, and the scope dependencies below turn
    a ``None`` into the right refusal for the route that needed one.
    """
    presented = _bearer_token(authorization)
    if presented:
        principal = ApiKeyManager(db).resolve(presented)
        if principal is not None:
            return principal
        # A malformed or revoked key is not silently downgraded to the
        # shared-secret path: someone presenting a key means to use it,
        # and quietly succeeding as a different identity would put the
        # wrong name in the audit trail.
        return None

    configured = get_settings().console_write_token
    if configured and x_console_token and hmac.compare_digest(x_console_token, configured):
        return Principal(
            name=x_actor or ANONYMOUS_ACTOR,
            scopes=frozenset({SCOPE_WRITE, SCOPE_READ}),
            via_shared_secret=True,
        )
    return None


def _any_keys_exist(db: Session) -> bool:
    return (
        db.execute(select(ApiKey.id).where(ApiKey.revoked_at.is_(None)).limit(1))
        .scalars()
        .first()
        is not None
    )


def _require(scope: str, principal: Principal | None, db: Session) -> Principal:
    if principal is None:
        if not get_settings().console_write_token and not _any_keys_exist(db):
            raise HTTPException(
                status_code=503,
                detail=(
                    "This deployment cannot authenticate anyone: no API keys "
                    "exist and CONSOLE_WRITE_TOKEN is not configured. Mint a "
                    "first key with `python -m mlops_framework.auth.cli` or "
                    "set CONSOLE_WRITE_TOKEN."
                ),
            )
        raise HTTPException(
            status_code=401,
            detail=(
                "Authentication required: send `Authorization: Bearer "
                f"<api key>` or the {HEADER_NAME} header."
            ),
        )
    if not principal.has(scope):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{principal.name!r} does not have the {scope!r} scope "
                f"(has: {sorted(principal.scopes)})."
            ),
        )
    return principal


def require_read(
    principal: Principal | None = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Principal:
    """Guard a route that reads privileged data."""
    return _require(SCOPE_READ, principal, db)


def require_write(
    principal: Principal | None = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Principal:
    """Guard a route that changes state."""
    return _require(SCOPE_WRITE, principal, db)


def require_admin(
    principal: Principal | None = Depends(get_principal),
    db: Session = Depends(get_db),
) -> Principal:
    """Guard a route that manages credentials."""
    return _require(SCOPE_ADMIN, principal, db)


# Kept as the name every existing route imports. It is now the `write`
# scope check rather than a bare shared-secret comparison; the routes
# guarded by it are unchanged, and so is what they refuse.
require_write_token = require_write


def get_actor(principal: Principal | None = Depends(get_principal)) -> str:
    """Who to record in the audit trail for this request.

    Supersedes ``deps.get_actor``, which read the caller's own
    ``X-Actor`` header and believed it. With an API key the name comes
    from the key row, so the audit trail finally records something the
    caller could not simply assert.

    A shared-secret request still resolves to its ``X-Actor`` value (or
    ``system``) — see this module's docstring on why that path is kept.
    ``Principal.via_shared_secret`` is what tells the two apart for
    anyone auditing the audit trail itself.
    """
    return principal.name if principal is not None else ANONYMOUS_ACTOR
