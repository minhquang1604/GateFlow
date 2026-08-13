"""Minimal write-gate for the small set of Gateflow endpoints that mutate
an *external* system (Airflow) rather than only the framework's own
database through its managers.

Not real authentication — there is still no user/session/RBAC concept
anywhere in this app (see ``airflow_views.py``'s module docstring, which
is the reason this module exists: exposing a write route to Airflow with
nothing in front of the console would let anyone who can reach it act on
production pipelines). This is a single shared secret, checked against one
request header, that an operator must opt into by setting
``CONSOLE_WRITE_TOKEN``. Unset — the default — every route guarded by
:func:`require_write_token` stays disabled, so introducing this module
does not by itself open up anything that was closed before it.

Read-only proxies (``mlflow_views.py``, most of ``airflow_views.py``) do
not use this — only routes that reach out and change state in another
system need it. Mutations to the framework's *own* database go through
the existing managers/routers unguarded, same as today; broadening this
gate to cover those too is a separate decision, not implied by this file.
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

    Raises:
        HTTPException: 503 if ``CONSOLE_WRITE_TOKEN`` is not configured
            (writes are off by default — a missing header is not the
            caller's fault when there is nothing to send), 401 if the
            header is missing while a token *is* configured, 403 if it
            does not match.
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
