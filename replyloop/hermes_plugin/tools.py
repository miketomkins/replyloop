"""JSON tool handlers for the optional Hermes ReplyLoop plugin."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from replyloop import cli as standalone
from replyloop.db import connect
from replyloop.service import ReminderService

from .delivery import HermesDeliveryAdapter, redact_text


def make_handler(fn: Callable[[dict[str, Any], Any], dict[str, Any]], ctx: Any = None) -> Callable[..., str]:
    def handler(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
        merged = dict(args or {})
        merged.update(kwargs)
        try:
            return json.dumps(fn(merged, ctx), sort_keys=True)
        except Exception as exc:
            return json.dumps({"ok": False, "error": redact_text(exc)}, sort_keys=True)

    return handler


def create(args: dict[str, Any], _ctx: Any = None) -> dict[str, Any]:
    ns = argparse.Namespace(
        db=args.get("db"),
        schedule_json=json.dumps(args["schedule"]) if isinstance(args.get("schedule"), dict) else None,
        once_at=args.get("once_at"),
        daily=bool(args.get("daily")),
        weekly=bool(args.get("weekly")),
        times=args.get("times") or [],
        weekdays=args.get("weekdays") or [],
        target=json.dumps(args["target"]) if isinstance(args.get("target"), dict) else None,
        platform=args.get("platform"),
        chat_id=args.get("chat_id"),
        sender_id=args.get("sender_id"),
        chat_type=args.get("chat_type") or "dm",
        reminder_id=args.get("id"),
        timezone=args.get("timezone") or "UTC",
        snooze=args.get("snooze") or 60,
        escalation=args.get("escalation") or [],
        max_deliveries=args.get("max_deliveries") or 1,
        repeat_last=bool(args.get("repeat_last")),
    )
    with standalone.connect_for_command(_db_path(args)) as db:
        return {"ok": True, **standalone.create_reminder(db, ns)}


def list_reminders(args: dict[str, Any], _ctx: Any = None) -> dict[str, Any]:
    with standalone.connect_for_command(_db_path(args)) as db:
        return {"ok": True, "reminders": [standalone.row_to_public_reminder(row) for row in standalone.list_reminder_rows(db, args.get("status"))]}


def get(args: dict[str, Any], _ctx: Any = None) -> dict[str, Any]:
    with standalone.connect_for_command(_db_path(args)) as db:
        return {"ok": True, **standalone.show_reminder(db, str(args["id"]))}


def pause(args: dict[str, Any], _ctx: Any = None) -> dict[str, Any]:
    return _set_status(args, "pause")


def resume(args: dict[str, Any], _ctx: Any = None) -> dict[str, Any]:
    return _set_status(args, "resume")


def cancel(args: dict[str, Any], _ctx: Any = None) -> dict[str, Any]:
    return _set_status(args, "cancel")


def tick(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
    if ctx is None:
        raise ValueError("Hermes PluginContext is required for delivery")
    adapter = HermesDeliveryAdapter(ctx)
    with connect(_db_path(args)) as db:
        result = ReminderService(db, adapter).tick()
    return {"ok": result.failed == 0, "tick": asdict(result)}


def doctor(args: dict[str, Any], _ctx: Any = None) -> dict[str, Any]:
    return {"ok": True, **standalone.doctor(_db_path(args))}


def _set_status(args: dict[str, Any], command: str) -> dict[str, Any]:
    with standalone.connect_for_command(_db_path(args)) as db:
        return {"ok": True, **standalone.set_status(db, str(args["id"]), command)}


def _db_path(args: dict[str, Any]) -> Path:
    return standalone.resolve_db_path(args.get("db"))
