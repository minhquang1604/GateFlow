"""API-key management — the ``admin`` scope's whole surface.

Deliberately small: mint, list, revoke. There is no update route,
because the only mutable things about a key are its scopes and its
existence, and silently widening a live credential's powers is worse
than revoking it and minting a replacement someone had to distribute
on purpose.

The plaintext appears in exactly one response body, ever — the one that
created it. See ``database/models/api_key.py``.

Bootstrapping: these routes need ``admin``, so the *first* key cannot be
minted through them. ``python -m mlops_framework.auth.cli`` does that
against the database directly, which is the same trust boundary as
running a migration.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from mlops_framework.api.deps import get_audit_manager, get_db
from mlops_framework.api.security import get_actor, require_admin
from mlops_framework.audit.manager import AuditManager
from mlops_framework.auth.manager import VALID_SCOPES, ApiKeyManager
from mlops_framework.exceptions import ApiKeyError

router = APIRouter(dependencies=[Depends(require_admin)])


class CreateApiKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    description: str | None = None


class ApiKeyOut(BaseModel):
    id: int
    name: str
    key_prefix: str
    scopes: list[str]
    description: str | None = None
    created_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None

    @classmethod
    def from_row(cls, row: object) -> ApiKeyOut:
        try:
            scopes = json.loads(getattr(row, "scopes_json", "") or "[]")
        except (TypeError, ValueError):
            scopes = []

        def _iso(value: object) -> str | None:
            return value.isoformat() if value is not None else None  # type: ignore[union-attr]

        return cls(
            id=row.id,  # type: ignore[attr-defined]
            name=row.name,  # type: ignore[attr-defined]
            key_prefix=row.key_prefix,  # type: ignore[attr-defined]
            scopes=scopes,
            description=row.description,  # type: ignore[attr-defined]
            created_at=_iso(getattr(row, "created_at", None)),
            last_used_at=_iso(getattr(row, "last_used_at", None)),
            revoked_at=_iso(getattr(row, "revoked_at", None)),
        )


class CreatedApiKeyOut(ApiKeyOut):
    # The only response that ever carries it. Nothing can return it
    # again, including this endpoint on a subsequent call.
    key: str


@router.post("/api-keys", response_model=CreatedApiKeyOut, status_code=201)
def create_api_key(
    request: CreateApiKeyRequest,
    db: Session = Depends(get_db),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> CreatedApiKeyOut:
    """Mint a key. The plaintext is in this response and nowhere else."""
    manager = ApiKeyManager(db)
    try:
        minted = manager.create(
            name=request.name,
            scopes=request.scopes,
            description=request.description,
        )
    except ApiKeyError as exc:
        # 409 for a name clash, 422 for an unusable request.
        status = 409 if "already exists" in str(exc) else 422
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    am.record(
        actor=actor,
        action="API_KEY_CREATED",
        entity_type="ApiKey",
        entity_id=minted.id,
        metadata={"name": minted.name, "scopes": minted.scopes},
    )
    row = manager.get_by_name(minted.name)
    out = ApiKeyOut.from_row(row)
    return CreatedApiKeyOut(**out.model_dump(), key=minted.plaintext)


@router.get("/api-keys", response_model=list[ApiKeyOut])
def list_api_keys(
    include_revoked: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[ApiKeyOut]:
    """List keys. Never returns a usable credential — only the prefix."""
    return [
        ApiKeyOut.from_row(row)
        for row in ApiKeyManager(db).list_keys(include_revoked=include_revoked)
    ]


@router.delete("/api-keys/{name}", response_model=ApiKeyOut)
def revoke_api_key(
    name: str,
    db: Session = Depends(get_db),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> ApiKeyOut:
    """Revoke a key.

    DELETE, but the row survives with ``revoked_at`` set: a key that
    acted has to stay resolvable for as long as the audit rows naming it
    do. Idempotent — re-revoking keeps the original timestamp.
    """
    try:
        row = ApiKeyManager(db).revoke(name)
    except ApiKeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    am.record(
        actor=actor,
        action="API_KEY_REVOKED",
        entity_type="ApiKey",
        entity_id=row.id,
        metadata={"name": row.name},
    )
    return ApiKeyOut.from_row(row)


@router.get("/api-keys/scopes", response_model=list[str])
def list_scopes() -> list[str]:
    """The scopes a key may be granted — so a console form does not have
    to hardcode a list that can drift from ``auth/manager.py``."""
    return sorted(VALID_SCOPES)
