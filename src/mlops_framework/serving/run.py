"""Runtime entrypoint for the FastAPI ServingBridge.

The bridge lives in :mod:`mlops_framework.serving.bridge`; this
module is a thin wrapper that the docker-compose ``serving`` service
launches with ``python -m mlops_framework.serving.run``.

Usage::

    python -m mlops_framework.serving.run --host 0.0.0.0 --port 8001
"""

from __future__ import annotations

import argparse

import uvicorn

from mlops_framework.database.session import DatabaseManager
from mlops_framework.serving.bridge import ServingBridge


def _build_app() -> "ServingBridge":
    """Build a ServingBridge that persists reloads to the framework DB."""
    db = DatabaseManager()

    def session_factory():
        return db.session_factory()

    return ServingBridge(session_factory=session_factory)


def main() -> None:
    parser = argparse.ArgumentParser(description="MLOps Framework ServingBridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()
    bridge = _build_app()
    uvicorn.run(bridge.app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()