"""Authentication and authorization — scoped API keys."""

from mlops_framework.auth.manager import (
    SCOPE_ADMIN,
    SCOPE_READ,
    SCOPE_WRITE,
    VALID_SCOPES,
    ApiKeyManager,
    MintedKey,
    Principal,
    effective_scopes,
)

__all__ = [
    "ApiKeyManager",
    "MintedKey",
    "Principal",
    "SCOPE_ADMIN",
    "SCOPE_READ",
    "SCOPE_WRITE",
    "VALID_SCOPES",
    "effective_scopes",
]
