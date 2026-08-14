"""Backwards-compatible shim.

``TelegramApprovalGate`` moved into the framework proper
(``mlops_framework.approval.telegram``) when human approval stopped
being a demo script's private concern and became an ``ApprovalGate``
the retraining workflow can be given — see that package's docstring.

This module stays so ``scripts/run_drift_recovery_demo.py`` and anyone
else importing it keep working, and re-exports the framework's version
rather than holding a second copy that could drift from it.
"""

from __future__ import annotations

from mlops_framework.approval.base import ApprovalDecision, ApprovalRequest
from mlops_framework.approval.telegram import TelegramApprovalGate

# The old name for the old shape. New code should use ApprovalDecision.
ApprovalResult = ApprovalDecision

__all__ = [
    "ApprovalDecision",
    "ApprovalRequest",
    "ApprovalResult",
    "TelegramApprovalGate",
]
