"""Telegram implementation of :class:`~mlops_framework.approval.base.ApprovalGate`.

Sends an Approve/Deny prompt to one admin chat and blocks until it is
answered or the timeout elapses. This began life inside a single demo
script, where it worked but was reachable only from there; the
behaviour is unchanged now that it is part of the framework.

``httpx`` is imported lazily and Telegram is never a hard dependency —
same treatment ``MLflowTracker`` gets. The framework must stay
importable, and testable, without a bot token in sight.

Deny by default: a timeout, an unreachable API, or a malformed reply
all produce ``approved=False``. See the ABC's module docstring.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from mlops_framework.approval.base import (
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
)

API_BASE = "https://api.telegram.org"


class TelegramApprovalGate(ApprovalGate):
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

    def request_approval(
        self, request: ApprovalRequest, *, timeout: float = 300.0
    ) -> ApprovalDecision:
        """Send the request with Approve/Deny buttons and block until the
        admin answers or ``timeout`` seconds elapse (deny-by-default).

        Any failure reaching Telegram is a denial, not an exception: a
        gate that could not ask anyone has not been told yes, and a
        raised error here would take down the retrain workflow that was
        merely trying to be polite about starting.
        """
        summary = request.summary
        try:
            after_update_id = self._latest_update_id()
        except Exception as exc:  # noqa: BLE001 - unreachable channel == denied
            return ApprovalDecision(
                approved=False, reason=f"could not reach Telegram: {exc}"
            )
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
            return ApprovalDecision(approved=False, reason="timeout — no admin response")

        decision, responder = answer
        if decision == "approve":
            self._edit_prompt(message_id, f"{summary}\n\n✅ *APPROVED* by {responder}.")
            return ApprovalDecision(approved=True, reason=f"approved by {responder}", responder=responder)

        self._edit_prompt(message_id, f"{summary}\n\n⛔ *DENIED* by {responder}.")
        return ApprovalDecision(approved=False, reason=f"denied by {responder}", responder=responder)
