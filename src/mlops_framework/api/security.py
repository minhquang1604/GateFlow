"""Write-gate for every endpoint that changes state — the framework's own
database included.

Not real authentication: there is still no user/session/RBAC concept
anywhere in this app. This is a single shared secret, checked against one
request header, that an operator must set as ``CONSOLE_WRITE_TOKEN``.
Unset, every route guarded by :func:`require_write_token` answers 503 —
it fails *closed*, so a deployment that has not been configured refuses
writes rather than accepting anonymous ones.

Scope
-----
This gate started life covering only the two Airflow task control routes
(clear/retry), on the reasoning that mutating an *external* system was
the dangerous case and that the framework's own tables could stay
unguarded. That reasoning did not hold up:

* ``POST /api/internal/models/{name}/promote`` puts a ModelVersion into
  PRODUCTION. Anyone who could reach the port could promote anything.
* ``POST /api/internal/training-runs/{id}/start`` triggers a real
  Airflow DAG run, so "only the framework's own database" was never
  accurate for that router either.
* ``POST /api/schedules`` + ``/run-now`` hands a ``pipeline_id`` to
  ``LocalDockerOrchestrator``, which imports and calls it. Combined with
  the source-splicing that used to live in ``orchestration/local.py``,
  that was remote code execution on the app container from an
  unauthenticated request.
* ``X-Actor`` (see ``api/deps.py``'s ``get_actor``) is caller-supplied
  and unverified, so an unguarded write route also made the audit trail
  it feeds trivially forgeable.

So every mutating route is now behind this dependency:
``/api/internal/*`` (whole router — its GET leaks dataset storage URIs
and belongs to the same machine-to-machine surface), and the write half
of ``/api/schedules``. Read-only proxies (``mlflow_views.py``, the read
half of ``airflow_views.py``, ``dashboard``/``datasets``/``models``/
``runs``/``lineage``/``audit``/``alerts``) are unchanged: they expose no
state change, and gating them would break the console's own rendering.

Replacing this with per-principal credentials and RBAC is still the
right end state; it is tracked separately. This closes the hole in the
meantime without pretending to be more than a shared secret.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from mlops_framework.config.settings import get_settings

HEADER_NAME = "X-Console-Token"


def require_write_token(
    x_console_token: str | None = Header(default=None, alias=HEADER_NAME),
) -> None:
    """FastAPI dependency guarding a write endpoint.

    Usable per-route (``dependencies=[Depends(require_write_token)]``) or
    for a whole router (``APIRouter(dependencies=[...])`` — how
    ``routers/internal.py`` applies it).

    Raises:
        HTTPException: 503 if ``CONSOLE_WRITE_TOKEN`` is not configured
            (writes are off until an operator sets one — a missing
            header is not the caller's fault when there is nothing to
            send), 401 if the header is missing while a token *is*
            configured, 403 if it does not match.
    """
    configured = get_settings().console_write_token
    if not configured:
        raise HTTPException(
            status_code=503,
            detail="Write endpoints are disabled: CONSOLE_WRITE_TOKEN is not configured.",
        )
    if not x_console_token:
        raise HTTPException(status_code=401, detail=f"Missing {HEADER_NAME} header.")
    if not hmac.compare_digest(x_console_token, configured):
        raise HTTPException(status_code=403, detail="Invalid write token.")
