"""FastAPI serving bridge for the MLOps framework.

The serving bridge exposes a small HTTP API that:

    1. Receives framework-level "model promoted" events.
    2. Validates the payload.
    3. Reloads the model *atomically* (the registry swap is a single
       reference assignment under a lock).
    4. Tracks the currently active model version per model.
    5. Optionally persists reloads to the framework's database.

The actual model code is *not* evaluated here — the registry stores
metadata and any caller-supplied "loader" function. This keeps the
serving bridge framework-agnostic: it can wrap sklearn, pytorch,
xgboost, or a no-op.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from mlops_framework.database.models.model import Model as ModelRow
from mlops_framework.database.models.model_version import ModelVersion
from mlops_framework.database.models.serving_instance import ServingInstance
from mlops_framework.events.publisher import ModelPromotedEvent


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------- #
# Registry
# ---------------------------------------------------------------------- #


class ServingModelRegistry:
    """Thread-safe in-memory registry of currently loaded models.

    Stores the model-version *id* and any caller-supplied payload
    (e.g. the deserialized model object, a closure, or a metadata
    dict). Swaps are atomic: the public ``set_active`` and ``get_active``
    methods hold a lock so a request never sees a partially-loaded
    model.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # model_name -> {"model_id", "model_version_id", "model_version_number",
        #                "artifact_uri", "loaded_at", "payload"}
        self._active: dict[str, dict[str, Any]] = {}

    def set_active(
        self,
        *,
        model_name: str,
        model_id: int,
        model_version_id: int,
        model_version_number: int,
        artifact_uri: Optional[str] = None,
        payload: Any = None,
    ) -> dict[str, Any]:
        record = {
            "model_name": model_name,
            "model_id": int(model_id),
            "model_version_id": int(model_version_id),
            "model_version_number": int(model_version_number),
            "artifact_uri": artifact_uri,
            "loaded_at": _now().isoformat(),
            "payload": payload,
        }
        with self._lock:
            self._active[model_name] = record
        return record

    def get_active(self, model_name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            rec = self._active.get(model_name)
            if rec is None:
                return None
            # Return a shallow copy so callers can't mutate the registry
            return {**rec, "payload": rec["payload"]}

    def list_active(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {k: v for k, v in rec.items() if k != "payload"}
                for rec in self._active.values()
            ]


# ---------------------------------------------------------------------- #
# Request schema
# ---------------------------------------------------------------------- #


class ReloadRequest(BaseModel):
    """Body of POST /internal/model/reload."""

    model_name: str = Field(..., min_length=1, max_length=255)
    model_version: int = Field(..., ge=1)
    artifact_uri: Optional[str] = Field(None, max_length=512)
    metrics: Optional[dict[str, Any]] = None
    model_id: Optional[int] = Field(None, ge=1)
    model_version_id: Optional[int] = Field(None, ge=1)


# ---------------------------------------------------------------------- #
# Bridge
# ---------------------------------------------------------------------- #


class ServingBridge:
    """The serving bridge: a small FastAPI app exposing /internal/*.

    The bridge is intentionally framework-agnostic: it knows about
    model names, version numbers, and artifact URIs. It does *not*
    import ML libraries. Callers can register a custom ``loader``
    callable (e.g. ``lambda uri: joblib.load(uri)``) to materialize
    a model object.
    """

    def __init__(
        self,
        registry: Optional[ServingModelRegistry] = None,
        loader: Optional[Callable[[str], Any]] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        serving_instance_id: Optional[str] = None,
    ) -> None:
        self._registry = registry or ServingModelRegistry()
        self._loader = loader
        self._session_factory = session_factory
        self._serving_instance_id = (
            serving_instance_id or f"serving-{id(self)}"
        )
        self.app: FastAPI = self._build_app()

    # ------------------------------------------------------------------ #
    # App construction
    # ------------------------------------------------------------------ #

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="MLOps Framework — Serving Bridge")

        @app.get("/healthz")
        def healthz() -> dict[str, Any]:
            return {"status": "ok", "serving_instance_id": self._serving_instance_id}

        @app.get("/internal/model/active")
        def list_active() -> dict[str, Any]:
            return {"models": self._registry.list_active()}

        @app.get("/internal/model/active/{model_name}")
        def get_active(model_name: str) -> dict[str, Any]:
            rec = self._registry.get_active(model_name)
            if rec is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"model {model_name!r} not loaded",
                )
            return {k: v for k, v in rec.items() if k != "payload"}

        @app.post("/internal/model/reload")
        def reload(request: ReloadRequest) -> dict[str, Any]:
            return self._handle_reload(request)

        return app

    # ------------------------------------------------------------------ #
    # Reload logic
    # ------------------------------------------------------------------ #

    def _handle_reload(self, request: ReloadRequest) -> dict[str, Any]:
        # Atomic swap: build the new record first, then assign.
        payload: Any = None
        if self._loader is not None and request.artifact_uri:
            try:
                payload = self._loader(request.artifact_uri)
            except Exception as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"loader failed: {exc}",
                ) from exc

        # If the bridge has a session factory, persist the reload.
        model_id = request.model_id
        model_version_id = request.model_version_id
        if self._session_factory is not None:
            resolved = self._persist_reload(
                model_name=request.model_name,
                model_version_number=request.model_version,
                artifact_uri=request.artifact_uri,
                model_id=model_id,
                model_version_id=model_version_id,
            )
            model_id = resolved["model_id"]
            model_version_id = resolved["model_version_id"]

        if model_id is None or model_version_id is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "model_id and model_version_id are required when "
                    "no session_factory is configured"
                ),
            )

        record = self._registry.set_active(
            model_name=request.model_name,
            model_id=model_id,
            model_version_id=model_version_id,
            model_version_number=request.model_version,
            artifact_uri=request.artifact_uri,
            payload=payload,
        )
        return {
            "status": "ok",
            "serving_instance_id": self._serving_instance_id,
            "model_name": record["model_name"],
            "model_version": record["model_version_number"],
            "loaded_at": record["loaded_at"],
        }

    def _persist_reload(
        self,
        *,
        model_name: str,
        model_version_number: int,
        artifact_uri: Optional[str],
        model_id: Optional[int],
        model_version_id: Optional[int],
    ) -> dict[str, int]:
        """Persist a reload to the framework's database.

        Returns the (resolved) model_id and model_version_id. If the
        caller did not supply them, we look them up by name + version.
        """
        if self._session_factory is None:
            return {"model_id": model_id, "model_version_id": model_version_id}

        session = self._session_factory()
        try:
            if model_id is None:
                row = session.query(ModelRow).filter_by(name=model_name).first()
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"model {model_name!r} not found",
                    )
                model_id = row.id
            if model_version_id is None:
                mv = (
                    session.query(ModelVersion)
                    .filter_by(model_id=model_id, version_number=model_version_number)
                    .first()
                )
                if mv is None:
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"model {model_name!r} v{model_version_number} not found"
                        ),
                    )
                model_version_id = mv.id
            # Mark any prior active reloads for this serving instance as
            # not active, then insert the new one.
            session.query(ServingInstance).filter_by(
                serving_instance_id=self._serving_instance_id,
                is_active=True,
            ).update({"is_active": False})
            si = ServingInstance(
                serving_instance_id=self._serving_instance_id,
                model_id=int(model_id),
                model_version_id=int(model_version_id),
                is_active=True,
                reload_source="event",
            )
            session.add(si)
            session.commit()
            return {
                "model_id": int(model_id),
                "model_version_id": int(model_version_id),
            }
        except HTTPException:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
