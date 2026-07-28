"""HTTP API for the MLOps Framework.

A thin FastAPI layer that exposes the existing managers as REST endpoints.
The API does not introduce new business logic — every endpoint delegates to
a Week 1-3 manager and serializes its return value.
"""

from mlops_framework.api.app import create_app

__all__ = ["create_app"]
