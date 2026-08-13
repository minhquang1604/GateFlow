"""Event publishing abstraction and reference implementation.

When a model is promoted to PRODUCTION, the framework publishes a
framework-level event so downstream consumers (serving bridge, audit
log, notification) can react. The event payload is intentionally
small and explicit.

This module provides:

    * :class:`EventPublisher`     — ABC for any transport.
    * :class:`InMemoryEventPublisher` — in-process; used by tests.
    * :class:`HttpEventPublisher` — POST a JSON payload to a webhook.

:class:`ModelPromotedEvent`, :class:`TrainingFailedEvent`,
:class:`DriftDetectedEvent` and :class:`RunBlockedEvent` are the
framework's typed event kinds. The latter three are also persisted to
the ``governance_events`` table by
:class:`mlops_framework.events.store.GovernanceEventStore` regardless
of whether an :class:`EventPublisher` is configured — see that
module's docstring for why persistence and webhook fan-out are
deliberately independent here, unlike :class:`ModelPromotedEvent`
(persisted to ``model_promotion_events`` only when a publisher exists;
see ``workflow/retraining.py::_publish_promotion``). Callers may
publish still other event types via :meth:`EventPublisher.publish_raw`
for future expansion.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------- #
# Events
# ---------------------------------------------------------------------- #


@dataclass
class Event:
    """A framework-level event.

    The framework emits :class:`Event` objects. Concrete subclasses
    (e.g. :class:`ModelPromotedEvent`) add type-specific fields.
    """

    event_type: str
    timestamp: str = field(default_factory=_now_iso)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class ModelPromotedEvent(Event):
    """Concrete event emitted when a ModelVersion is promoted to PRODUCTION.

    Fields follow the project spec:

        {
            "event_type": "MODEL_PROMOTED",
            "model_name": "...",
            "model_version": int,
            "artifact_uri": "...",
            "timestamp": "...",
        }
    """

    def __init__(
        self,
        model_name: str,
        model_version: int,
        artifact_uri: str | None = None,
        metrics: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "model_name": model_name,
            "model_version": int(model_version),
        }
        if artifact_uri is not None:
            payload["artifact_uri"] = artifact_uri
        if metrics is not None:
            payload["metrics"] = dict(metrics)
        super().__init__(
            event_type="MODEL_PROMOTED",
            timestamp=timestamp or _now_iso(),
            payload=payload,
        )


@dataclass
class TrainingFailedEvent(Event):
    """Emitted when a TrainingRun ends in FAILED — either the pipeline
    itself raised, or it finished in a non-SUCCESS state. See
    ``workflow/retraining.py`` (the in-process path) and
    ``api/routers/internal.py::finish_training_run`` (the Airflow-DAG
    callback path) — the only two places a TrainingRun ever reaches
    FAILED."""

    def __init__(
        self,
        training_run_id: int,
        pipeline_id: str | None = None,
        error_message: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"training_run_id": training_run_id}
        if pipeline_id is not None:
            payload["pipeline_id"] = pipeline_id
        if error_message is not None:
            payload["error_message"] = error_message
        super().__init__(
            event_type="TRAINING_FAILED",
            timestamp=timestamp or _now_iso(),
            payload=payload,
        )


@dataclass
class DriftDetectedEvent(Event):
    """Emitted when :class:`~mlops_framework.drift.detector.DriftService`
    evaluates a dataset version pair and finds drift. See
    ``workflow/retraining.py``'s drift-detection step."""

    def __init__(
        self,
        dataset_version_id: int,
        score: float | None = None,
        threshold: float | None = None,
        method: str | None = None,
        timestamp: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"dataset_version_id": dataset_version_id}
        if score is not None:
            payload["score"] = score
        if threshold is not None:
            payload["threshold"] = threshold
        if method is not None:
            payload["method"] = method
        super().__init__(
            event_type="DRIFT_DETECTED",
            timestamp=timestamp or _now_iso(),
            payload=payload,
        )


@dataclass
class RunBlockedEvent(Event):
    """Emitted when a retrain is blocked before training ever starts —
    a dataset version that failed readiness, or a model deemed not
    eligible to retrain. ``reason`` is a short machine code
    (``"readiness_blocked"`` / ``"not_eligible"``); ``reasons`` carries
    the engine's own human-readable explanation."""

    def __init__(
        self,
        reason: str,
        dataset_version_id: int | None = None,
        model_id: int | None = None,
        reasons: list[str] | None = None,
        timestamp: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"reason": reason}
        if dataset_version_id is not None:
            payload["dataset_version_id"] = dataset_version_id
        if model_id is not None:
            payload["model_id"] = model_id
        if reasons is not None:
            payload["reasons"] = list(reasons)
        super().__init__(
            event_type="RUN_BLOCKED",
            timestamp=timestamp or _now_iso(),
            payload=payload,
        )


# ---------------------------------------------------------------------- #
# ABC
# ---------------------------------------------------------------------- #


class EventPublisher(ABC):
    """Abstract event publisher.

    Implementations are responsible for delivering the event to the
    transport. They must return ``True`` on success and ``False`` on a
    non-fatal failure; raising is reserved for programming errors
    (e.g. misconfiguration).
    """

    @abstractmethod
    def publish(self, event: Event) -> bool:
        """Publish an :class:`Event` synchronously.

        Returns:
            True on successful delivery, False on failure.
        """
        raise NotImplementedError

    def publish_raw(self, event_type: str, payload: dict[str, Any]) -> bool:
        """Publish a generic event without a typed subclass."""
        return self.publish(
            Event(event_type=event_type, payload=payload)
        )


# ---------------------------------------------------------------------- #
# In-memory
# ---------------------------------------------------------------------- #


class InMemoryEventPublisher(EventPublisher):
    """Collects events in memory. Useful for tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> bool:
        self.events.append(event)
        return True


# ---------------------------------------------------------------------- #
# HTTP
# ---------------------------------------------------------------------- #


class HttpEventPublisher(EventPublisher):
    """Publish events to a webhook via HTTP POST.

    The first successful POST establishes the connection. The
    publisher is intentionally minimal — Redis / Kafka can be added
    later behind the same ABC.
    """

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 5.0,
        headers: dict[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not url:
            raise ValueError("HttpEventPublisher requires a url")
        self._url = url
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = dict(headers or {})
        # Content-Type default
        self._headers.setdefault("Content-Type", "application/json")

    def publish(self, event: Event) -> bool:
        try:
            response = self._client.post(
                self._url,
                content=event.to_json(),
                headers=self._headers,
            )
        except Exception:
            return False
        return 200 <= response.status_code < 300

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:
                pass
