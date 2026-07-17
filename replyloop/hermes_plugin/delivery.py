"""Hermes delivery adapter for ReplyLoop outbound reminders."""

from __future__ import annotations

import json
import re
from importlib import import_module
from typing import Any

from replyloop.delivery import DeliveryOutcome, DeliveryRequest

_TARGETISH_RE = re.compile(r"([+]?\d[\d .()\-]{5,}\d|[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+|[A-Za-z0-9_-]{12,})")


def redact_text(value: Any, known_targets: list[Any] | tuple[Any, ...] = ()) -> str:
    text = str(value or "")
    for target in known_targets:
        raw = str(target or "")
        if raw:
            text = text.replace(raw, "[redacted]")
    return _TARGETISH_RE.sub("[redacted]", text)


def redacted_label(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "unknown"
    import hashlib

    return "id:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:10]


class HermesDeliveryAdapter:
    """Delivery adapter that routes sends through Hermes' send_message tool."""

    transport = "hermes"

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.requests: list[DeliveryRequest] = []

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        self.requests.append(request)
        target = _format_target(request.target)
        send_args = {"action": "send", "target": target, "message": request.text}
        known_targets = _target_values(request.target, target)
        try:
            raw = _send_message(self.ctx, send_args)
        except Exception as exc:  # pragma: no cover - defensive; fakes cover normal failure shape
            return DeliveryOutcome.failure(self.transport, f"dispatch failed for {redacted_label(target)}: {redact_text(exc, known_targets)}")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            return DeliveryOutcome.failure(self.transport, f"invalid send_message response for {redacted_label(target)}")
        if not isinstance(payload, dict):
            return DeliveryOutcome.failure(self.transport, f"invalid send_message response for {redacted_label(target)}")
        if payload.get("success") is True:
            message_id = payload.get("message_id") or payload.get("id") or payload.get("provider_message_id")
            if not isinstance(message_id, str) or not message_id:
                message_id = f"hermes:{request.idempotency_key}"
            return DeliveryOutcome.success(self.transport, message_id)
        error = payload.get("error") or "transport did not report success"
        return DeliveryOutcome.failure(self.transport, f"send_message failed for {redacted_label(target)}: {redact_text(error, known_targets)}")


def _send_message(ctx: Any, args: dict[str, Any]) -> Any:
    """Dispatch through the supported Hermes send_message implementation.

    Current Hermes PluginContext.dispatch_tool only reaches registry-backed
    tools, and send_message is not registry-backed in some releases. Try the
    public plugin seam first for forward compatibility, then fall back to the
    existing send_message tool helper that Hermes itself uses.
    """

    dispatch_tool = getattr(ctx, "dispatch_tool", None)
    if callable(dispatch_tool):
        result = dispatch_tool("send_message", args)
        if not _is_unknown_tool_result(result):
            return result
    return _direct_send_message(args)


def _is_unknown_tool_result(result: Any) -> bool:
    try:
        payload = json.loads(result) if isinstance(result, str) else result
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    error = str(payload.get("error") or "").lower()
    return "unknown tool" in error and "send_message" in error


def _direct_send_message(args: dict[str, Any]) -> Any:
    try:
        send_message_tool = import_module("tools.send_message_tool").send_message_tool
    except Exception as exc:  # pragma: no cover - depends on Hermes checkout availability
        raise RuntimeError("Hermes send_message helper is unavailable") from exc
    return send_message_tool(args)


def _target_values(target: dict[str, Any], formatted: str) -> list[str]:
    values = [formatted]
    for key in ("platform", "chat_id", "sender_id", "user_id", "thread_id"):
        value = target.get(key)
        if value is not None:
            values.append(str(value))
    return values


def _format_target(target: dict[str, Any]) -> str:
    platform = str(target.get("platform") or "").strip().lower()
    chat = str(target.get("chat_id") or "").strip()
    if not platform or not chat:
        raise ValueError("target platform and chat_id are required")
    return f"{platform}:{chat}"
