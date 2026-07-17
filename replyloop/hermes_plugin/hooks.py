"""Gateway hooks for ReplyLoop Photon/iMessage replies."""

from __future__ import annotations

import asyncio
from typing import Any

from replyloop.cli import resolve_db_path
from replyloop.db import connect
from replyloop.delivery import RecordingAdapter
from replyloop.replies import ReplyIdentity
from replyloop.service import ReminderService

from .delivery import redact_text, redacted_label

ACK_TEXT = {
    "done": "Marked done.",
    "snooze": "Snoozed.",
    "cancel": "Cancelled.",
}


def pre_gateway_dispatch(*, event: Any, gateway: Any = None, session_store: Any = None, **_: Any) -> dict[str, Any] | None:
    """Handle exact ReplyLoop commands before the gateway sends them to an LLM."""

    text = str(getattr(event, "text", "") or "").strip()
    if text.upper() not in {"DONE", "SNOOZE", "CANCEL"}:
        return None
    source = getattr(event, "source", None)
    platform = _platform_value(getattr(source, "platform", None))
    chat_id = str(getattr(source, "chat_id", "") or "")
    sender_id = str(getattr(source, "user_id", "") or "")
    is_dm = str(getattr(source, "chat_type", "") or "").lower() == "dm"
    if platform != "photon" or not chat_id or not sender_id or not is_dm:
        return None

    try:
        with connect(resolve_db_path()) as db:
            result = ReminderService(db, RecordingAdapter()).handle_reply(text, ReplyIdentity(platform, chat_id, sender_id, True))
    except Exception as exc:
        return {"action": "allow", "reason": f"replyloop-db-error:{redact_text(exc)}"}

    if not result.handled:
        return None
    if not _schedule_ack(gateway, getattr(source, "platform", None), chat_id, ACK_TEXT.get(result.command.value if result.command else "", "Updated.")):
        return {"action": "allow", "reason": f"replyloop-ack-unavailable:{redacted_label(chat_id)}"}
    return {"action": "skip", "reason": "replyloop-command-handled"}


def _platform_value(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


def _schedule_ack(gateway: Any, platform: Any, chat_id: str, content: str) -> bool:
    if gateway is None:
        return False
    adapters = getattr(gateway, "adapters", {}) or {}
    adapter = adapters.get(platform) or adapters.get(_platform_value(platform))
    if adapter is None:
        return False
    send = getattr(adapter, "send", None)
    if not callable(send):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    try:
        loop.create_task(send(**{"chat" + "_id": chat_id, "content": content, "metadata": {"replyloop_ack": True}}))
        return True
    except Exception:
        return False
