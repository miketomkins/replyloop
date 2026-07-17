"""Domain models for reminders, occurrences, delivery attempts, and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ValidationError

UTC = timezone.utc


class ReminderStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class OccurrenceStatus(StrEnum):
    DUE = "due"
    DELIVERED = "delivered"
    DONE = "done"
    SNOOZED = "snoozed"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class Reminder:
    id: str
    target: str
    schedule: dict[str, Any]
    timezone: str
    status: ReminderStatus = ReminderStatus.ACTIVE
    default_snooze_minutes: int = 60
    escalation_minutes: tuple[int, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.id:
            raise ValidationError("reminder id is required")
        if not self.target:
            raise ValidationError("target is required")
        if not isinstance(self.schedule, dict):
            raise ValidationError("schedule must be a mapping")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError(f"unknown timezone: {self.timezone}") from exc
        if self.default_snooze_minutes <= 0:
            raise ValidationError("default_snooze_minutes must be positive")
        if any(value <= 0 for value in self.escalation_minutes):
            raise ValidationError("escalation_minutes must be positive")


@dataclass(frozen=True)
class Occurrence:
    id: str
    reminder_id: str
    scheduled_for: datetime
    status: OccurrenceStatus = OccurrenceStatus.DUE
    due_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.id:
            raise ValidationError("occurrence id is required")
        if not self.reminder_id:
            raise ValidationError("reminder id is required")
        _require_aware_utc(self.scheduled_for, "scheduled_for")
        if self.due_at is not None:
            _require_aware_utc(self.due_at, "due_at")


@dataclass(frozen=True)
class DeliveryAttempt:
    id: str
    occurrence_id: str
    attempted_at: datetime
    status: DeliveryStatus
    transport: str
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.id:
            raise ValidationError("delivery attempt id is required")
        if not self.occurrence_id:
            raise ValidationError("occurrence id is required")
        if not self.transport:
            raise ValidationError("transport is required")
        _require_aware_utc(self.attempted_at, "attempted_at")


@dataclass(frozen=True)
class Event:
    id: int | None
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.aggregate_type:
            raise ValidationError("aggregate_type is required")
        if not self.aggregate_id:
            raise ValidationError("aggregate_id is required")
        if not self.event_type:
            raise ValidationError("event_type is required")
        if not isinstance(self.payload, dict):
            raise ValidationError("payload must be a mapping")
        _require_aware_utc(self.created_at, "created_at")


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("datetime must be timezone aware")
    return value.astimezone(UTC)


def datetime_to_iso(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def datetime_from_iso(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    return to_utc(parsed)


def _require_aware_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be timezone aware")
    if value.utcoffset() != UTC.utcoffset(value):
        raise ValidationError(f"{name} must be UTC")
