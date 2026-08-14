"""FrameworkSettingsManager: persisted overrides for governance policy.

``PromotionConfig``, ``EligibilityConfig``, ``TrainingPolicy`` and
``DriftConfig`` (governance/promotion.py, governance/eligibility.py,
readiness/engine.py, drift/detector.py) each already round-trip through
``from_dict``/``to_dict`` — built for exactly this — but until now were
only ever constructed with hardcoded literals at their call sites
(``scheduling/runner.py``'s cron path, ``api/routers/internal.py``'s
manual promote/readiness endpoints), with no way to change a threshold
short of editing source and redeploying.

This manager is the single place that reads/writes the
``framework_settings`` table underneath those four dataclasses. It
lives under its own top-level package, not ``api/``, for the same
reason ``mlops_framework.audit`` does (see that module's docstring):
``scheduling/runner.py`` needs it directly and must not import
``mlops_framework.api``. Deliberately not named ``settings`` — that
name is already taken twice over, by the env-var-backed
``mlops_framework.config.settings`` (process config, unrelated) and the
read-only ``api/routers/settings.py`` diagnostics panel (which stays
read-only; this is a different layer).

A key with no row means "use the dataclass's own bare default" — rows
are only created once something is actually customized, so an empty
table is behaviourally identical to this manager not existing at all.
Callers that need the *effective* config for one of the four policies
should use the typed ``get_*_config()``/``get_training_policy()``
helpers below rather than ``get_raw()`` directly.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mlops_framework.database.models.framework_setting import FrameworkSetting
from mlops_framework.drift.detector import DriftConfig
from mlops_framework.governance.eligibility import EligibilityConfig
from mlops_framework.governance.promotion import PromotionConfig
from mlops_framework.readiness.engine import TrainingPolicy

PROMOTION = "promotion"
ELIGIBILITY = "eligibility"
TRAINING_POLICY = "training_policy"
DRIFT = "drift"

_DATACLASS_FOR_KEY: dict[str, type] = {
    PROMOTION: PromotionConfig,
    ELIGIBILITY: EligibilityConfig,
    TRAINING_POLICY: TrainingPolicy,
    DRIFT: DriftConfig,
}


class FrameworkSettingsManager:
    """Get/set the persisted override for each of the four policy keys.

    Every dataclass here already tolerates unknown-shaped or missing
    data via its own ``from_dict`` (returns bare defaults on ``None``,
    ignores keys it doesn't recognise) — ``set_raw`` reuses that same
    method as its validate-and-normalize step, so a malformed value
    raises the same ``TypeError``/``ValueError`` a caller constructing
    the dataclass directly would get, rather than this manager
    reimplementing field-by-field validation.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # Raw key/value access
    # ------------------------------------------------------------------ #

    def get_raw(self, key: str) -> dict[str, Any] | None:
        """The persisted override for ``key``, or ``None`` if unset.
        Raises ``KeyError`` for a key outside the known four — a typo'd
        key with no row would otherwise silently read back as "unset"
        instead of surfacing the mistake."""
        self._dataclass_for(key)  # validates key, raising KeyError early
        row = self._row(key)
        return json.loads(row.value_json) if row is not None else None

    def set_raw(self, key: str, value: dict[str, Any]) -> dict[str, Any]:
        """Validate ``value`` against ``key``'s dataclass and persist it.

        Raises ``KeyError`` for an unknown key, and whatever
        ``TypeError``/``ValueError`` the dataclass's own ``from_dict``
        raises for a malformed value — both are the caller's to turn
        into an HTTP 4xx. Returns the *normalized* value actually
        stored (``from_dict(value).to_dict()``), which may differ
        cosmetically from ``value`` (e.g. a list coerced from a tuple).
        """
        dataclass_type = self._dataclass_for(key)
        normalized = dataclass_type.from_dict(value).to_dict()
        value_json = json.dumps(normalized)

        row = self._row(key)
        if row is None:
            row = FrameworkSetting(key=key, value_json=value_json)
            self._session.add(row)
        else:
            row.value_json = value_json
        self._session.flush()
        return normalized

    def reset(self, key: str) -> None:
        """Delete the persisted override for ``key`` (a no-op if unset),
        reverting it to the dataclass's own bare default."""
        self._dataclass_for(key)  # validates key, raising KeyError early
        row = self._row(key)
        if row is not None:
            self._session.delete(row)
            self._session.flush()

    def list_effective(self) -> dict[str, dict[str, Any]]:
        """Every key's effective value (persisted override, or the bare
        default when unset) plus whether it's customized — the shape
        the Settings page reads directly."""
        return {
            key: {
                "value": self._effective_dict(key),
                "is_default": self.get_raw(key) is None,
            }
            for key in _DATACLASS_FOR_KEY
        }

    # ------------------------------------------------------------------ #
    # Typed accessors — what every other call site should actually use
    # ------------------------------------------------------------------ #

    def get_promotion_config(self) -> PromotionConfig:
        return PromotionConfig.from_dict(self.get_raw(PROMOTION))

    def get_eligibility_config(self) -> EligibilityConfig:
        return EligibilityConfig.from_dict(self.get_raw(ELIGIBILITY))

    def get_training_policy(self) -> TrainingPolicy:
        return TrainingPolicy.from_dict(self.get_raw(TRAINING_POLICY))

    def get_drift_config(self) -> DriftConfig:
        return DriftConfig.from_dict(self.get_raw(DRIFT))

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    @staticmethod
    def _dataclass_for(key: str) -> type:
        try:
            return _DATACLASS_FOR_KEY[key]
        except KeyError:
            raise KeyError(
                f"unknown framework settings key {key!r}; "
                f"expected one of {sorted(_DATACLASS_FOR_KEY)}"
            ) from None

    def _row(self, key: str) -> FrameworkSetting | None:
        return self._session.execute(
            select(FrameworkSetting).where(FrameworkSetting.key == key)
        ).scalars().first()

    def _effective_dict(self, key: str) -> dict[str, Any]:
        raw = self.get_raw(key)
        dataclass_type = _DATACLASS_FOR_KEY[key]
        return dataclass_type.from_dict(raw).to_dict()
