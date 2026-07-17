"""Event helpers for ReplyLoop's append-only history."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .db import ReplyLoopDB
from .models import Event


def append_event(db: ReplyLoopDB, aggregate_type: str, aggregate_id: str, event_type: str, payload: dict[str, Any]) -> Event:
    """Append one event outside a projection mutation."""
    return db.append_event(Event(None, aggregate_type, aggregate_id, event_type, payload))


def events_for(db: ReplyLoopDB, aggregate_type: str, aggregate_id: str) -> list[Event]:
    """Return events for an aggregate in append order."""
    return db.list_events(aggregate_type, aggregate_id)


def event_types(events: Iterable[Event]) -> list[str]:
    """Extract event type names from events while preserving order."""
    return [event.event_type for event in events]
