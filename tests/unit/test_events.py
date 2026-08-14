"""Unit tests for the event publisher abstraction and types."""

from __future__ import annotations

import json

import httpx
import pytest

from mlops_framework.events.publisher import (
    DriftDetectedEvent,
    HttpEventPublisher,
    InMemoryEventPublisher,
    ModelPromotedEvent,
    RunBlockedEvent,
    TrainingFailedEvent,
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


class TestTrainingFailedEvent:
    def test_payload_shape(self):
        e = TrainingFailedEvent(training_run_id=42, pipeline_id="p", error_message="boom")
        d = e.to_dict()
        assert d["event_type"] == "TRAINING_FAILED"
        assert d["payload"] == {
            "training_run_id": 42, "pipeline_id": "p", "error_message": "boom",
        }

    def test_optional_fields_omitted_when_none(self):
        e = TrainingFailedEvent(training_run_id=42)
        assert e.to_dict()["payload"] == {"training_run_id": 42}

    def test_is_json_serializable(self):
        json.dumps(TrainingFailedEvent(training_run_id=1).to_dict())


class TestDriftDetectedEvent:
    def test_payload_shape(self):
        e = DriftDetectedEvent(dataset_version_id=7, score=0.9, threshold=0.05, method="ks")
        d = e.to_dict()
        assert d["event_type"] == "DRIFT_DETECTED"
        assert d["payload"] == {
            "dataset_version_id": 7, "score": 0.9, "threshold": 0.05, "method": "ks",
        }

    def test_is_json_serializable(self):
        json.dumps(DriftDetectedEvent(dataset_version_id=1).to_dict())


class TestRunBlockedEvent:
    def test_payload_shape(self):
        e = RunBlockedEvent(
            reason="not_eligible", dataset_version_id=3, model_id=9, reasons=["too soon"],
        )
        d = e.to_dict()
        assert d["event_type"] == "RUN_BLOCKED"
        assert d["payload"] == {
            "reason": "not_eligible", "dataset_version_id": 3, "model_id": 9,
            "reasons": ["too soon"],
        }

    def test_is_json_serializable(self):
        json.dumps(RunBlockedEvent(reason="readiness_blocked").to_dict())


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
