"""Unit tests for ``mlops_framework.approval.telegram.TelegramApprovalGate``.

The gate moved out of a single demo script and into the
framework when human approval became an ``ApprovalGate`` the retraining
workflow can be given — these tests moved with it, unchanged in what
they cover: the polling loop, and that a click from a chat other than
the configured admin is ignored.

No real network calls — httpx.post is monkeypatched to a small fake
Telegram Bot API that plays back a scripted sequence of responses, so
these run fast and hermetically (the live end-to-end check against the
real bot/chat happened out-of-band during development, not here).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mlops_framework.approval import ApprovalRequest
from mlops_framework.approval import telegram as approval_module
from mlops_framework.approval.telegram import TelegramApprovalGate

ADMIN_CHAT_ID = "111"
OTHER_CHAT_ID = "999"


class FakeTelegram:
    """Scripted stand-in for the Telegram Bot HTTP API.

    ``getUpdates_queue`` is a list of "batches"; each call to
    getUpdates pops and returns the next batch (an empty list if
    exhausted), so a test can script exactly what the poll loop sees
    call-by-call.
    """

    def __init__(self, getUpdates_queue: list[list[dict]]) -> None:
        self.getUpdates_queue = list(getUpdates_queue)
        self.sent_messages: list[dict] = []
        self.edits: list[dict] = []
        self.answered_callbacks: list[str] = []
        self._next_update_id = 1000

    def post(self, url: str, json: dict, timeout: float):
        method = url.rsplit("/", 1)[-1]
        if method == "sendMessage":
            self.sent_messages.append(json)
            return _FakeResponse({"ok": True, "result": {"message_id": 42}})
        if method == "editMessageText":
            self.edits.append(json)
            return _FakeResponse({"ok": True, "result": {}})
        if method == "answerCallbackQuery":
            self.answered_callbacks.append(json["callback_query_id"])
            return _FakeResponse({"ok": True, "result": True})
        if method == "getUpdates":
            batch = self.getUpdates_queue.pop(0) if self.getUpdates_queue else []
            return _FakeResponse({"ok": True, "result": batch})
        raise AssertionError(f"unexpected Telegram method: {method}")


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _callback_update(update_id: int, *, message_id: int, from_id: str, data: str, username: str = "someone") -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cbq{update_id}",
            "data": data,
            "from": {"id": int(from_id), "username": username},
            "message": {"message_id": message_id},
        },
    }


@pytest.fixture
def gate(monkeypatch):
    fake = FakeTelegram(getUpdates_queue=[])
    monkeypatch.setattr(approval_module.httpx, "post", fake.post)
    g = TelegramApprovalGate(bot_token="TEST-TOKEN", admin_chat_id=ADMIN_CHAT_ID)
    return g, fake


class TestApprove:
    def test_approve_from_admin(self, gate):
        g, fake = gate
        fake.getUpdates_queue = [
            [],  # the drain call before sending the prompt
            [_callback_update(1, message_id=42, from_id=ADMIN_CHAT_ID, data="approve", username="Kang_NFT")],
        ]
        result = g.request_approval(ApprovalRequest(summary="drift detected"), timeout=5.0)
        assert result.approved is True
        assert result.responder == "Kang_NFT"
        assert "approved by Kang_NFT" in result.reason
        # the outcome gets written back onto the original message
        assert any("APPROVED" in e["text"] for e in fake.edits)
        # the button click was ack'd so Telegram stops the loading spinner
        assert fake.answered_callbacks == ["cbq1"]


class TestDeny:
    def test_deny_from_admin(self, gate):
        g, fake = gate
        fake.getUpdates_queue = [
            [],
            [_callback_update(1, message_id=42, from_id=ADMIN_CHAT_ID, data="deny")],
        ]
        result = g.request_approval(ApprovalRequest(summary="drift detected"), timeout=5.0)
        assert result.approved is False
        assert "denied by" in result.reason
        assert any("DENIED" in e["text"] for e in fake.edits)


class TestTimeout:
    def test_no_response_denies_by_default(self, gate):
        g, fake = gate
        fake.getUpdates_queue = []  # never any update
        result = g.request_approval(ApprovalRequest(summary="drift detected"), timeout=0.05)
        assert result.approved is False
        assert "timeout" in result.reason
        assert any("TIMED OUT" in e["text"] for e in fake.edits)


class TestForeignChatIgnored:
    def test_click_from_someone_else_is_ignored_then_admin_approves(self, gate):
        g, fake = gate
        fake.getUpdates_queue = [
            [],
            # A click from someone who isn't the admin — must not count.
            [_callback_update(1, message_id=42, from_id=OTHER_CHAT_ID, data="approve", username="stranger")],
            # The real admin then approves.
            [_callback_update(2, message_id=42, from_id=ADMIN_CHAT_ID, data="approve", username="Kang_NFT")],
        ]
        result = g.request_approval(ApprovalRequest(summary="drift detected"), timeout=5.0)
        assert result.approved is True
        assert result.responder == "Kang_NFT"

    def test_only_foreign_clicks_times_out(self, gate):
        g, fake = gate
        fake.getUpdates_queue = [
            [],
            [_callback_update(1, message_id=42, from_id=OTHER_CHAT_ID, data="approve")],
        ]
        result = g.request_approval(ApprovalRequest(summary="drift detected"), timeout=0.05)
        assert result.approved is False
        assert "timeout" in result.reason


class TestFromSettings:
    def test_requires_both_token_and_chat_id(self):
        settings = SimpleNamespace(telegram_bot_token=None, telegram_admin_chat_id=None)
        with pytest.raises(ValueError):
            TelegramApprovalGate.from_settings(settings)

    def test_builds_from_settings(self):
        settings = SimpleNamespace(telegram_bot_token="T", telegram_admin_chat_id="123")
        g = TelegramApprovalGate.from_settings(settings)
        assert g._chat_id == "123"
