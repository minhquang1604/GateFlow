"""API keys — the framework's first real notion of *who* is calling.

Until this existed there was one shared secret (``CONSOLE_WRITE_TOKEN``)
and an ``X-Actor`` header the caller filled in itself. That stopped
anonymous writes, which was the urgent problem, but it could not tell
two authorized callers apart, and the audit trail recorded whatever the
caller claimed. "Who promoted this model" was answerable only as far as
you trusted the person answering.

A key row holds a **hash**, never the key. The plaintext is returned
once, at creation, and is unrecoverable afterwards — a leaked database
dump then yields no usable credential, and "I lost it" is a rotation
rather than a lookup.

``scopes`` is a JSON array rather than a foreign key to a roles table:
there are three of them (``read``, ``write``, ``admin``), they are
properties of the key rather than of a person, and a join table would be
ceremony around a set that fits in a line. See
``mlops_framework.api.security`` for what each one gates.

Revocation is a timestamp, not a delete. A key that acted needs to stay
resolvable for as long as the audit rows naming it do.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from mlops_framework.database.base import Base, TimestampMixin


class ApiKey(Base, TimestampMixin):
    """A named, scoped credential."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Who this key acts as. Lands in AuditLog.actor, verified rather
    # than claimed — which is the entire point of this table.
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    # sha256 of the plaintext. Indexed because every authenticated
    # request looks a key up by exactly this.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # The leading identifiable chunk of the key ("mlops_ak_A1b2c3"), so
    # a human can match a row to the key in their password manager
    # without the row being enough to authenticate with.
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    scopes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        state = "revoked" if self.revoked_at else "active"
        return f"<ApiKey {self.name!r} {self.key_prefix}… {state}>"
