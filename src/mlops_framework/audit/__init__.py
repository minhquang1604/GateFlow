"""Audit trail — a persisted record of who triggered which action.

See :mod:`mlops_framework.audit.manager` and
:class:`mlops_framework.database.models.audit_log.AuditLog`.
"""

from mlops_framework.audit.manager import AuditManager

__all__ = ["AuditManager"]
