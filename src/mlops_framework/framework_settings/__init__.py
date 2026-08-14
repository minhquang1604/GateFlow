"""Persisted overrides for the framework's governance dataclasses.

See :class:`mlops_framework.framework_settings.manager.FrameworkSettingsManager`.
"""

from __future__ import annotations

from mlops_framework.framework_settings.manager import (
    DRIFT,
    ELIGIBILITY,
    PROMOTION,
    TRAINING_POLICY,
    FrameworkSettingsManager,
)

__all__ = [
    "DRIFT",
    "ELIGIBILITY",
    "PROMOTION",
    "TRAINING_POLICY",
    "FrameworkSettingsManager",
]
