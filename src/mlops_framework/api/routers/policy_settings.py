"""Policy settings router — view/edit the persisted governance overrides.

Deliberately a separate file from ``api/routers/settings.py``, which
stays read-only/diagnostic (DB/MLflow/Airflow connectivity, app info —
see that module). This router is the API surface over
:class:`~mlops_framework.framework_settings.manager.FrameworkSettingsManager`:
the four governance dataclasses (promotion, eligibility, training
readiness, drift) that used to only exist as hardcoded literals at
their call sites (``scheduling/runner.py``, this package's
``internal.py``) and now have a persisted, editable default.

Not gated by ``api/security.py``'s ``require_write_token`` — that gate
is scoped to routes that mutate an *external* system (its own
docstring says so); this mutates the framework's own DB, same category
as every other unguarded route in ``internal.py``. Accountability here
comes from the mandatory audit row every write leaves on the Activity
page instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from mlops_framework.api.deps import (
    get_actor,
    get_audit_manager,
    get_framework_settings_manager,
)
from mlops_framework.audit.manager import AuditManager
from mlops_framework.framework_settings.manager import FrameworkSettingsManager

router = APIRouter()


class PolicyEntry(BaseModel):
    value: dict[str, Any]
    is_default: bool


class SetPolicyRequest(BaseModel):
    value: dict[str, Any]


@router.get("/settings/policies", response_model=dict[str, PolicyEntry])
def list_policies(
    mgr: FrameworkSettingsManager = Depends(get_framework_settings_manager),
) -> dict[str, PolicyEntry]:
    """Every policy's effective value (persisted override, or the bare
    dataclass default when unset) plus whether it's been customized."""
    return {
        key: PolicyEntry(**entry) for key, entry in mgr.list_effective().items()
    }


@router.get("/settings/policies/{key}", response_model=PolicyEntry)
def get_policy(
    key: str,
    mgr: FrameworkSettingsManager = Depends(get_framework_settings_manager),
) -> PolicyEntry:
    try:
        mgr.get_raw(key)  # raises KeyError with a well-formed message for an unknown key
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PolicyEntry(**mgr.list_effective()[key])


@router.put("/settings/policies/{key}", response_model=PolicyEntry)
def set_policy(
    key: str,
    request: SetPolicyRequest,
    mgr: FrameworkSettingsManager = Depends(get_framework_settings_manager),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> PolicyEntry:
    """Validate ``request.value`` (round-tripped through the policy's
    own ``from_dict``/``to_dict`` — the same validation constructing
    the dataclass directly would apply) and persist it."""
    try:
        normalized = mgr.set_raw(key, request.value)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    am.record(
        actor=actor,
        action="SETTINGS_UPDATED",
        entity_type="FrameworkSetting",
        metadata={"key": key, "value": normalized},
    )
    return PolicyEntry(value=normalized, is_default=False)


@router.post("/settings/policies/{key}/reset", response_model=PolicyEntry)
def reset_policy(
    key: str,
    mgr: FrameworkSettingsManager = Depends(get_framework_settings_manager),
    am: AuditManager = Depends(get_audit_manager),
    actor: str = Depends(get_actor),
) -> PolicyEntry:
    """Delete the persisted override, reverting ``key`` to its bare
    dataclass default."""
    try:
        mgr.reset(key)
        default_value = mgr.list_effective()[key]["value"]
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    am.record(
        actor=actor,
        action="SETTINGS_RESET",
        entity_type="FrameworkSetting",
        metadata={"key": key},
    )
    return PolicyEntry(value=default_value, is_default=True)
