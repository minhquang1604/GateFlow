"""Settings — a single read-only pane over the framework's effective
configuration and the live reachability of the systems it talks to.

Before this, "is MLflow even pointed at the right tracking server" or "is
Airflow reachable at all" meant opening a `.env` file and then, separately,
either UI to see if it responded. This answers both from Gateflow itself.

Secrets (the Airflow password, any credential embedded in ``DATABASE_URL``)
are masked in the response — this endpoint is meant for "what am I pointed
at", not a credential dump. There is still no auth layer in front of the
console (see ``airflow_views.py``'s module docstring), so masking is the
only protection a value here gets; nothing genuinely secret should be
readable through this endpoint even so.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from mlops_framework.api import airflow_gateway, mlflow_gateway
from mlops_framework.config.settings import get_settings

router = APIRouter()

_MASK = "••••••••"


def _mask_secret(value: str | None) -> str | None:
    """Replace a secret with a fixed-width placeholder, preserving
    "is this set at all" (``None`` stays ``None``) without leaking length."""
    return _MASK if value else value


def _mask_database_url(url: str) -> str:
    """Mask the password segment of a SQLAlchemy URL, keeping the rest
    (scheme, user, host, database) visible — that is what answers "which
    database am I pointed at"; the password never should be visible here."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, tail = rest.split("@", 1)
    if ":" in creds:
        user, _pw = creds.split(":", 1)
        creds = f"{user}:{_MASK}"
    return f"{scheme}://{creds}@{tail}"


class SystemSettings(BaseModel):
    configured: bool
    reachable: bool
    reason: str | None = None
    fields: dict[str, Any]


class SettingsOut(BaseModel):
    app_name: str
    app_version: str
    database: SystemSettings
    mlflow: SystemSettings
    airflow: SystemSettings
    scheduler: dict[str, Any]


def _probe_mlflow() -> tuple[bool, str | None]:
    """Reuse ``mlflow_gateway.panel`` — the same degrade-don't-fail
    contract every other MLflow-backed view already relies on — with a
    cheap call (``search_experiments(max_results=1)``) whose only purpose
    here is "did the tracking server answer", not its result."""
    result = mlflow_gateway.panel(lambda client: client.search_experiments(max_results=1))
    return result.available, result.reason


def _probe_airflow() -> tuple[bool, str | None]:
    """Same idea as :func:`_probe_mlflow`, via ``airflow_gateway.panel``
    and the health endpoint every Airflow-backed view already calls."""
    result = airflow_gateway.panel(lambda o: o.get_health())
    return result.available, result.reason


@router.get("/settings", response_model=SettingsOut)
def get_settings_panel() -> SettingsOut:
    """Effective config for the database, MLflow and Airflow, secrets
    masked, plus a live reachability ping for the latter two."""
    settings = get_settings()

    database = SystemSettings(
        configured=True,
        reachable=True,
        fields={"url": _mask_database_url(settings.database_url)},
    )

    mlflow_reachable, mlflow_reason = _probe_mlflow()
    mlflow = SystemSettings(
        configured=bool(settings.mlflow_tracking_uri),
        reachable=mlflow_reachable,
        reason=mlflow_reason,
        fields={
            "tracking_uri": settings.mlflow_tracking_uri,
            "experiment_name": settings.mlflow_experiment_name,
            "s3_endpoint_url": settings.mlflow_s3_endpoint_url,
        },
    )

    airflow_reachable, airflow_reason = _probe_airflow()
    airflow = SystemSettings(
        configured=bool(settings.airflow_base_url),
        reachable=airflow_reachable,
        reason=airflow_reason,
        fields={
            "base_url": settings.airflow_base_url,
            "username": settings.airflow_username,
            "password": _mask_secret(settings.airflow_password),
        },
    )

    return SettingsOut(
        app_name=settings.app_name,
        app_version=settings.app_version,
        database=database,
        mlflow=mlflow,
        airflow=airflow,
        scheduler={
            "enabled": settings.scheduler_enabled,
            "poll_seconds": settings.scheduler_poll_seconds,
        },
    )
