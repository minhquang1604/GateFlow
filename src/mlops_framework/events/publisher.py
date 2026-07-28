"""Event publishing abstraction and reference implementation.

When a model is promoted to PRODUCTION, the framework publishes a
framework-level event so downstream consumers (serving bridge, audit
log, notification) can react. The event payload is intentionally
small and explicit.

This module provides:

    * :class:`EventPublisher`     — ABC for any transport.
    * :class:`InMemoryEventPublisher` — in-process; used by tests.
    * :class:`HttpEventPublisher` — POST a JSON payload to a webhook.

The :class:`ModelPromotedEvent` dataclass is the framework-level
event type. Callers may publish other event types via
:meth:`EventPublisher.publish_raw` for future expansion.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import httpx


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        artifact_uri: Optional[str] = None,
        metrics: Optional[dict[str, Any]] = None,
        timestamp: Optional[str] = None,
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
        headers: Optional[dict[str, str]] = None,
        client: Optional[httpx.Client] = None,
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
