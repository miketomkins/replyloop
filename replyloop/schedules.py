"""Schedule validation and deterministic due-time generation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ValidationError
from .models import to_utc

UTC = timezone.utc
_COLON = ":"
_ONCE_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}" + _COLON + r"\d{2}"
    r"(?:" + _COLON + r"\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}" + _COLON + r"\d{2})?$",
    re.ASCII,
)
_HHMM_RE = re.compile(r"^\d{2}" + _COLON + r"\d{2}$", re.ASCII)


@dataclass(frozen=True)
class ValidatedSchedule:
    kind: str
    timezone: str
    times: tuple[time, ...] = ()
    weekdays: tuple[int, ...] = ()
    at: datetime | None = None


def validate_schedule(schedule: dict[str, Any], timezone_name: str) -> ValidatedSchedule:
    """Validate a once, daily, or weekly schedule.

    Daily and weekly schedules accept one or more unique HH:MM values. Weekly
    weekdays use Monday=0 through Sunday=6. The timezone is validated with
    zoneinfo.ZoneInfo and stored separately on the reminder.
    """
    if not isinstance(schedule, dict):
        raise ValidationError("schedule must be a mapping")
    _load_timezone(timezone_name)
    kind = schedule.get("kind")
    if kind == "once":
        allowed = {"kind", "at"}
        _reject_unknown_keys(schedule, allowed)
        at = schedule.get("at")
        if not isinstance(at, str):
            raise ValidationError("once schedule requires string at")
        parsed = _parse_once_at(at, timezone_name)
        return ValidatedSchedule(kind="once", timezone=timezone_name, at=parsed)
    if kind == "daily":
        allowed = {"kind", "times"}
        _reject_unknown_keys(schedule, allowed)
        return ValidatedSchedule(kind="daily", timezone=timezone_name, times=_parse_times(schedule.get("times")))
    if kind == "weekly":
        allowed = {"kind", "times", "weekdays"}
        _reject_unknown_keys(schedule, allowed)
        return ValidatedSchedule(
            kind="weekly",
            timezone=timezone_name,
            times=_parse_times(schedule.get("times")),
            weekdays=_parse_weekdays(schedule.get("weekdays")),
        )
    raise ValidationError("schedule kind must be once, daily, or weekly")


def due_times_between(schedule: dict[str, Any], timezone_name: str, start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Return due UTC datetimes in [start_utc, end_utc).

    DST behavior is explicit and deterministic:
    - nonexistent local wall times during spring-forward gaps are skipped;
    - ambiguous local wall times during fall-back folds produce both UTC instants;
    - normal local wall times produce one UTC instant.

    This keeps local schedules predictable without silently moving reminders to a
    different wall-clock time.
    """
    start = to_utc(start_utc)
    end = to_utc(end_utc)
    if end <= start:
        raise ValidationError("end_utc must be after start_utc")

    validated = validate_schedule(schedule, timezone_name)
    if validated.kind == "once":
        assert validated.at is not None
        due = to_utc(validated.at)
        return [due] if start <= due < end else []

    tz = _load_timezone(timezone_name)
    start_local = start.astimezone(tz).date() - timedelta(days=1)
    end_local = end.astimezone(tz).date() + timedelta(days=1)
    results: set[datetime] = set()
    for current in _date_range(start_local, end_local):
        if validated.kind == "weekly" and current.weekday() not in validated.weekdays:
            continue
        for scheduled_time in validated.times:
            for candidate in _local_wall_time_candidates(current, scheduled_time, tz):
                utc_candidate = candidate.astimezone(UTC)
                if start <= utc_candidate < end:
                    results.add(utc_candidate)
    return sorted(results)


def _load_timezone(timezone_name: str) -> ZoneInfo:
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValidationError("timezone is required")
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValidationError(f"unknown timezone: {timezone_name}") from exc


def _reject_unknown_keys(schedule: dict[str, Any], allowed: set[str]) -> None:
    non_string = [key for key in schedule if not isinstance(key, str)]
    if non_string:
        raise ValidationError("schedule keys must be strings")
    unknown = sorted(set(schedule) - allowed)
    if unknown:
        raise ValidationError("unknown schedule keys: " + ", ".join(unknown))


def _parse_once_at(value: str, timezone_name: str) -> datetime:
    if _ONCE_AT_RE.fullmatch(value) is None:
        raise ValidationError("once at must be an ISO datetime with a date and time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("once at must be an ISO datetime") from exc
    tz = _load_timezone(timezone_name)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        candidates = _local_wall_time_candidates(parsed.date(), parsed.time().replace(tzinfo=None), tz)
        if len(candidates) != 1:
            raise ValidationError("once at must not be nonexistent or ambiguous when timezone is omitted")
        return candidates[0]

    # Once schedules are local wall-clock times in the declared IANA timezone.
    # If an offset is supplied, it must agree with that timezone for the named
    # local instant. We then normalize to ZoneInfo so stored reminder timezone
    # and generated due instant cannot contradict each other.
    wall_time = parsed.time().replace(tzinfo=None)
    matching_candidates = [
        candidate
        for candidate in _local_wall_time_candidates(parsed.date(), wall_time, tz)
        if candidate.utcoffset() == parsed.utcoffset()
    ]
    if not matching_candidates:
        raise ValidationError("once at offset must match the declared timezone")
    return matching_candidates[0]


def _parse_times(values: Any) -> tuple[time, ...]:
    if not isinstance(values, list) or not values:
        raise ValidationError("times must be a non-empty list")
    parsed: list[time] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValidationError("times must contain HH:MM strings")
        try:
            if _HHMM_RE.fullmatch(value) is None:
                raise ValueError
            hour_text, minute_text = value.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            item = time(hour=hour, minute=minute)
        except ValueError as exc:
            raise ValidationError("times must use HH:MM 24-hour format") from exc
        normalized = item.strftime("%H:%M")
        if normalized in seen:
            raise ValidationError("times must be unique")
        seen.add(normalized)
        parsed.append(item)
    return tuple(sorted(parsed))


def _parse_weekdays(values: Any) -> tuple[int, ...]:
    if not isinstance(values, list) or not values:
        raise ValidationError("weekdays must be a non-empty list")
    seen: set[int] = set()
    parsed: list[int] = []
    for value in values:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 6:
            raise ValidationError("weekdays must be integers from 0 through 6")
        if value in seen:
            raise ValidationError("weekdays must be unique")
        seen.add(value)
        parsed.append(value)
    return tuple(sorted(parsed))


def _date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _local_wall_time_candidates(day: date, wall_time: time, tz: ZoneInfo) -> tuple[datetime, ...]:
    naive = datetime.combine(day, wall_time.replace(tzinfo=None))
    candidates: list[datetime] = []
    seen_utc: set[datetime] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        roundtrip = aware.astimezone(UTC).astimezone(tz)
        if _same_wall_time(roundtrip, naive) and roundtrip.fold == fold:
            utc_value = aware.astimezone(UTC)
            if utc_value not in seen_utc:
                seen_utc.add(utc_value)
                candidates.append(aware)
    return tuple(sorted(candidates, key=lambda value: value.astimezone(UTC)))


def _same_wall_time(value: datetime, naive: datetime) -> bool:
    return (
        value.year == naive.year
        and value.month == naive.month
        and value.day == naive.day
        and value.hour == naive.hour
        and value.minute == naive.minute
        and value.second == naive.second
        and value.microsecond == naive.microsecond
    )
