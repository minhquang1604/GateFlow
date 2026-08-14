"""Telegram admin-approval gate for the drift/retrain demo.

``scripts/run_drift_recovery_demo.py`` uses this to block before an
automated retrain: it sends the admin an Approve/Deny message over
Telegram and waits for a real button press before letting
``RetrainingWorkflow.run()`` proceed.

This is deliberately a small, dependency-free wrapper around the
Telegram Bot HTTP API (https://core.telegram.org/bots/api) — no
python-telegram-bot / aiogram, just ``httpx`` + long-polling
``getUpdates``, since the demo only needs one request/response
round-trip.

Security notes:
    * Only a callback_query whose ``from.id`` matches the configured
      admin chat_id is honored — a click from anyone else who might
      also be in the chat (or, for a group chat, a different member)
      is ignored, not treated as approval.
    * The bot token and chat_id are read from ``Settings``
      (environment / ``.env``), never hardcoded here.

Usage::

    gate = TelegramApprovalGate.from_settings(settings)
    result = gate.request_approval(
        "Drift detected on `credit-card-fraud-drift-demo` ...",
        timeout=settings.telegram_approval_timeout_seconds,
    )
    if not result.approved:
        raise SystemExit(f"retrain blocked: {result.reason}")
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

API_BASE = "https://api.telegram.org"


@dataclass
class ApprovalResult:
    approved: bool
    reason: str
    responder: str | None = None  # Telegram display name / username of whoever answered


class TelegramApprovalGate:
    """Sends an Approve/Deny prompt and blocks until answered or timed out."""

    def __init__(self, bot_token: str, admin_chat_id: str) -> None:
        if not bot_token or not admin_chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID must both be set "
                "(see .env.example) to use the retrain approval gate."
            )
        self._token = bot_token
        self._chat_id = str(admin_chat_id)
        self._api = f"{API_BASE}/bot{bot_token}"

    @classmethod
    def from_settings(cls, settings: Any) -> TelegramApprovalGate:
        return cls(
            bot_token=settings.telegram_bot_token,
            admin_chat_id=settings.telegram_admin_chat_id,
        )

    # ------------------------------------------------------------------ #
    # Low-level Bot API calls
    # ------------------------------------------------------------------ #

    def _call(self, method: str, **params: Any) -> dict:
        # getUpdates is a long-poll: Telegram holds the connection open for
        # up to params["timeout"] seconds waiting for an update, so the
        # httpx-side timeout must be comfortably longer than that — a flat
        # 10s here would abort the read before Telegram ever responds.
        http_timeout = params.get("timeout", 0) + 10.0
        resp = httpx.post(f"{self._api}/{method}", json=params, timeout=http_timeout)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {body}")
        return body["result"]

    def _send_prompt(self, text: str) -> int:
        """Send the Approve/Deny message, return its message_id."""
        result = self._call(
            "sendMessage",
            chat_id=self._chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve retrain", "callback_data": "approve"},
                        {"text": "⛔ Deny retrain", "callback_data": "deny"},
                    ]
                ]
            },
        )
        return result["message_id"]

    def _edit_prompt(self, message_id: int, text: str) -> None:
        """Best-effort — replaces the buttons with the final outcome so the
        chat history shows what was decided, without live buttons hanging
        around to be clicked again."""
        try:
            self._call(
                "editMessageText",
                chat_id=self._chat_id,
                message_id=message_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    def _latest_update_id(self) -> int:
        """Drain currently-queued updates so a stale button press from a
        previous run isn't mistaken for an answer to *this* prompt."""
        updates = self._call("getUpdates", timeout=0)
        return updates[-1]["update_id"] if updates else -1

    def _poll_answer(
        self, message_id: int, after_update_id: int, deadline: float
    ) -> tuple[str, str] | None:
        """Poll getUpdates for a callback_query on ``message_id`` from the
        admin chat. Returns (decision, responder_name) or None on timeout."""
        offset = after_update_id + 1
        while time.time() < deadline:
            remaining = max(1, min(20, int(deadline - time.time())))
            updates = self._call("getUpdates", offset=offset, timeout=remaining)
            for update in updates:
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue
                if cq.get("message", {}).get("message_id") != message_id:
                    continue
                from_id = str(cq.get("from", {}).get("id"))
                # Always ack the callback so Telegram stops showing the
                # client-side "loading" spinner on the button.
                try:
                    self._call("answerCallbackQuery", callback_query_id=cq["id"])
                except Exception:
                    pass
                if from_id != self._chat_id:
                    # Someone else in the chat clicked — ignore, keep waiting.
                    continue
                responder = cq.get("from", {}).get("username") or cq.get("from", {}).get(
                    "first_name", "admin"
                )
                return cq["data"], responder
        return None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def request_approval(self, summary: str, *, timeout: float = 300.0) -> ApprovalResult:
        """Send ``summary`` with Approve/Deny buttons and block until the
        admin answers or ``timeout`` seconds elapse (deny-by-default)."""
        after_update_id = self._latest_update_id()
        message_id = self._send_prompt(
            f"{summary}\n\n_Waiting up to {int(timeout)}s for a decision…_"
        )
        deadline = time.time() + timeout

        answer = self._poll_answer(message_id, after_update_id, deadline)

        if answer is None:
            self._edit_prompt(
                message_id,
                f"{summary}\n\n⏱ *TIMED OUT* — no response within {int(timeout)}s. Retrain cancelled.",
            )
            return ApprovalResult(approved=False, reason="timeout — no admin response")

        decision, responder = answer
        if decision == "approve":
            self._edit_prompt(message_id, f"{summary}\n\n✅ *APPROVED* by {responder}.")
            return ApprovalResult(approved=True, reason=f"approved by {responder}", responder=responder)

        self._edit_prompt(message_id, f"{summary}\n\n⛔ *DENIED* by {responder}.")
        return ApprovalResult(approved=False, reason=f"denied by {responder}", responder=responder)
