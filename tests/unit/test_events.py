"""Unit tests for the event publisher abstraction and types."""

from __future__ import annotations

import json

import httpx
import pytest

from mlops_framework.events.publisher import (
    Event,
    HttpEventPublisher,
    InMemoryEventPublisher,
    ModelPromotedEvent,
)


class TestModelPromotedEvent:
    def test_payload_shape(self):
        e = ModelPromotedEvent(
            model_name="fraud-model",
            model_version=3,
            artifact_uri="s3://models/fraud-v3.pkl",
            metrics={"f1": 0.9},
        )
        d = e.to_dict()
        assert d["event_type"] == "MODEL_PROMOTED"
        assert d["payload"]["model_name"] == "fraud-model"
        assert d["payload"]["model_version"] == 3
        assert d["payload"]["artifact_uri"] == "s3://models/fraud-v3.pkl"
        assert d["payload"]["metrics"] == {"f1": 0.9}
        assert "timestamp" in d

    def test_is_json_serializable(self):
        e = ModelPromotedEvent("m", 1, "uri")
        json.dumps(e.to_dict())


class TestInMemoryEventPublisher:
    def test_publish_collects(self):
        pub = InMemoryEventPublisher()
        assert pub.publish(ModelPromotedEvent("m", 1)) is True
        assert len(pub.events) == 1
        assert pub.events[0].event_type == "MODEL_PROMOTED"

    def test_publish_raw(self):
        pub = InMemoryEventPublisher()
        pub.publish_raw("CUSTOM", {"x": 1})
        assert pub.events[0].event_type == "CUSTOM"
        assert pub.events[0].payload == {"x": 1}


class TestHttpEventPublisher:
    def _make_client(self) -> tuple[httpx.Client, list[tuple[str, str, str, dict]]]:
        """Build a fake httpx.Client and capture POSTs."""
        captured: list[tuple[str, str, str, dict]] = []

        class _Resp:
            def __init__(self, code: int) -> None:
                self.status_code = code

        class _Client:
            def post(self, url, content=None, headers=None):
                captured.append(("POST", url, content, headers))
                return _Resp(200)

            def close(self):
                pass

        return _Client(), captured  # type: ignore

    def test_successful_post(self):
        client, captured = self._make_client()
        pub = HttpEventPublisher("http://localhost/reload", client=client)  # type: ignore
        e = ModelPromotedEvent("m", 2, "uri")
        assert pub.publish(e) is True
        assert len(captured) == 1
        method, url, body, _ = captured[0]
        assert method == "POST"
        assert url == "http://localhost/reload"
        parsed = json.loads(body)
        assert parsed["event_type"] == "MODEL_PROMOTED"
        assert parsed["payload"]["model_version"] == 2

    def test_failed_post_returns_false(self):
        class _Resp:
            status_code = 500

        class _Client:
            def post(self, url, content=None, headers=None):
                return _Resp()

            def close(self):
                pass

        pub = HttpEventPublisher("http://localhost/reload", client=_Client())  # type: ignore
        assert pub.publish(ModelPromotedEvent("m", 1)) is False

    def test_url_required(self):
        with pytest.raises(ValueError):
            HttpEventPublisher("")
