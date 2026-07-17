"""Hermes tool schemas for the optional ReplyLoop plugin."""

from __future__ import annotations

from typing import Any

TOOLSET = "replyloop"

_COMMON_PROPERTIES: dict[str, Any] = {
    "db": {"type": "string", "description": "SQLite database path. Defaults to REPLYLOOP_DB or XDG data home."},
}

CREATE_SCHEMA = {
    "name": "replyloop_create",
    "description": "Create a local ReplyLoop reminder.",
    "parameters": {
        "type": "object",
        "properties": {
            **_COMMON_PROPERTIES,
            "id": {"type": "string"},
            "schedule": {"type": "object"},
            "once_at": {"type": "string"},
            "daily": {"type": "boolean"},
            "weekly": {"type": "boolean"},
            "times": {"type": "array", "items": {"type": "string"}},
            "weekdays": {"type": "array", "items": {"type": "integer"}},
            "timezone": {"type": "string"},
            "target": {"type": "object"},
            "platform": {"type": "string"},
            "chat_id": {"type": "string"},
            "sender_id": {"type": "string"},
            "chat_type": {"type": "string", "enum": ["dm", "group"]},
            "snooze": {"type": "integer"},
            "escalation": {"type": "array", "items": {"type": "integer"}},
            "max_deliveries": {"type": "integer"},
            "repeat_last": {"type": "boolean"},
        },
        "required": [],
    },
}

LIST_SCHEMA = {
    "name": "replyloop_list",
    "description": "List ReplyLoop reminders.",
    "parameters": {"type": "object", "properties": {**_COMMON_PROPERTIES, "status": {"type": "string", "enum": ["active", "paused", "cancelled"]}}, "required": []},
}

GET_SCHEMA = {
    "name": "replyloop_get",
    "description": "Get one ReplyLoop reminder and its occurrences.",
    "parameters": {"type": "object", "properties": {**_COMMON_PROPERTIES, "id": {"type": "string"}}, "required": ["id"]},
}

STATUS_SCHEMA = {
    "parameters": {"type": "object", "properties": {**_COMMON_PROPERTIES, "id": {"type": "string"}}, "required": ["id"]},
}

TICK_SCHEMA = {
    "name": "replyloop_tick",
    "description": "Create and deliver due ReplyLoop reminders through Hermes send_message.",
    "parameters": {"type": "object", "properties": {**_COMMON_PROPERTIES}, "required": []},
}

DOCTOR_SCHEMA = {
    "name": "replyloop_doctor",
    "description": "Run ReplyLoop operational diagnostics.",
    "parameters": {"type": "object", "properties": {**_COMMON_PROPERTIES}, "required": []},
}

def status_schema(name: str, description: str) -> dict[str, Any]:
    return {"name": name, "description": description, **STATUS_SCHEMA}
