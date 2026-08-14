"""Human approval as a framework concern, not a demo script's.

A retrain that a policy says should happen is not always a retrain
someone wants to happen unattended. The framework already models every
*machine* decision — readiness, drift, eligibility, promotion — with an
explainable result; a human's decision was the one gap, and it lived
only in ``scripts/_telegram_approval.py``, wired by hand into one demo.

This is the same shape as :class:`~mlops_framework.drift.detector.DriftDetector`
and :class:`~mlops_framework.events.publisher.EventPublisher`: an ABC
the framework depends on, adapters that do not depend on it. Telegram is
a reference implementation, not the contract.

Deny by default
---------------
:meth:`ApprovalGate.request_approval` returns a decision, never raises
to mean "no", and a channel that times out or fails must return
``approved=False``. A gate that could not reach anyone has not been
told yes, and treating unreachability as consent would make the gate
worse than not having one — it would fail open at exactly the moment
something is already wrong.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApprovalRequest:
    """What the approver is being asked to decide.

    ``summary`` is prose for a human. ``context`` carries the structured
    facts a channel may render (model name, drift score, metrics) and
    that the audit trail records alongside the answer.
    """

    summary: str
    action: str = "retrain"
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalDecision:
    """An explainable answer, in the shape every other framework
    decision uses (see ``PromotionDecision``, ``ReadinessResult``)."""

    approved: bool
    reason: str = ""
    # Who answered, when a channel can tell. None for gates where the
    # question of identity does not arise (auto-approval in a test).
    responder: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reason": self.reason,
            "responder": self.responder,
        }


class ApprovalGate(ABC):
    """Ask a human, and report what they said."""

    @abstractmethod
    def request_approval(
        self, request: ApprovalRequest, *, timeout: float = 3600.0
    ) -> ApprovalDecision:
        """Block until answered or ``timeout`` elapses.

        Implementations must return ``ApprovalDecision(approved=False)``
        on timeout or channel failure rather than raising — see the
        module docstring on why the default is deny.
        """


class AutoApproveGate(ApprovalGate):
    """Always approves. For tests, and for a caller that wants the
    audit record of a gate without a human in the loop.

    Named for what it does. A gate that silently approved would be
    indistinguishable from no gate at all in a code review; this one is
    obvious at the call site.
    """

    def __init__(self, responder: str = "auto-approve") -> None:
        self._responder = responder

    def request_approval(
        self, request: ApprovalRequest, *, timeout: float = 3600.0
    ) -> ApprovalDecision:
        return ApprovalDecision(
            approved=True,
            reason="auto-approved (no human in the loop)",
            responder=self._responder,
        )


class DenyAllGate(ApprovalGate):
    """Always denies. The other half of the pair — makes "the gate was
    consulted and said no" testable without a channel."""

    def request_approval(
        self, request: ApprovalRequest, *, timeout: float = 3600.0
    ) -> ApprovalDecision:
        return ApprovalDecision(
            approved=False, reason="denied by DenyAllGate", responder="deny-all"
        )
