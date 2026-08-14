"""Human approval gates — an ABC plus adapters.

See ``base.py`` for why this is a framework concern rather than a demo
script's, and why every gate denies by default.
"""

from mlops_framework.approval.base import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    AutoApproveGate,
    DenyAllGate,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalRequest",
    "AutoApproveGate",
    "DenyAllGate",
]
