"""Transactional reminder lifecycle service."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .clock import Clock, RealClock
from .db import ReplyLoopDB
from .delivery import DeliveryAdapter, DeliveryOutcome, DeliveryRequest, OutcomeStatus
from .errors import ValidationError
from .models import DeliveryAttempt, DeliveryStatus, Event, Occurrence, OccurrenceStatus, Reminder, ReminderStatus, datetime_from_iso, datetime_to_iso, to_utc
from .replies import ParsedReply, ReplyCommand, ReplyIdentity, duration_delta, parse_reply, target_matches
from .schedules import due_times_between, validate_schedule

_RETRY_MINUTES = (1, 5, 15)
_MAX_RETRY_MINUTES = 60
_MAX_REPLY_DURATION_MINUTES = 366 * 24 * 60
_DELIVERY_CLAIM_LEASE_MINUTES = 60


@dataclass(frozen=True)
class TickResult:
    created: int = 0
    attempted: int = 0
    delivered: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ReplyResult:
    handled: bool
    command: ReplyCommand | None = None
    occurrence_id: str | None = None
    reason: str | None = None


class ReminderService:
    def __init__(self, db: ReplyLoopDB, adapter: DeliveryAdapter, clock: Clock | None = None) -> None:
        self.db = db
        self.adapter = adapter
        self.clock = clock or RealClock()

    def create_reminder(
        self,
        *,
        reminder_id: str,
        target: dict[str, Any],
        schedule: dict[str, Any],
        timezone: str,
        default_snooze_minutes: int = 60,
        intervals_minutes: tuple[int, ...] = (),
        max_deliveries: int = 1,
        repeat_last: bool = False,
    ) -> Reminder:
        _validate_target(target)
        validate_schedule(schedule, timezone)
        _validate_duration_minutes(default_snooze_minutes, "default_snooze_minutes")
        _validate_intervals(intervals_minutes, max_deliveries)
        if not isinstance(repeat_last, bool):
            raise ValidationError("repeat_last must be a boolean")
        stored_schedule = dict(schedule)
        stored_schedule["_replyloop"] = {"max_deliveries": max_deliveries, "repeat_last": repeat_last}
        now = self.clock.now()
        reminder = Reminder(
            id=reminder_id,
            target=json.dumps(target, sort_keys=True, separators=(",", ":")),
            schedule=stored_schedule,
            timezone=timezone,
            default_snooze_minutes=default_snooze_minutes,
            escalation_minutes=tuple(intervals_minutes),
            created_at=now,
            updated_at=now,
        )
        self.db.add_reminder(reminder)
        return reminder

    def tick(self) -> TickResult:
        now = self.clock.now()
        self._recover_stale_claims(now)
        created = self._create_due_occurrences(now)
        due = self.db.list_due_occurrences(now + timedelta(microseconds=1))
        attempted = delivered = failed = 0
        for occurrence in due:
            claim_at = self.clock.now()
            reminder = self.db.get_reminder(occurrence.reminder_id)
            if reminder is None or reminder.status != ReminderStatus.ACTIVE:
                continue
            if not self._transport_due(occurrence.id, claim_at):
                continue
            claim_id = self._claim_occurrence(occurrence.id, claim_at)
            if claim_id is None:
                continue
            idempotency_key = self._delivery_idempotency_key(occurrence.id)
            attempted += 1
            try:
                outcome = self.adapter.deliver(
                    DeliveryRequest(
                        occurrence.id,
                        reminder.id,
                        idempotency_key,
                        _decode_target(reminder.target),
                        _message_for(reminder, occurrence),
                    )
                )
            except Exception as exc:
                outcome = DeliveryOutcome.failure(getattr(self.adapter, "transport", "unknown"), str(exc) or exc.__class__.__name__)
            outcome_at = self.clock.now()
            try:
                if outcome.status == OutcomeStatus.SUCCESS:
                    if self._record_success(reminder, occurrence, outcome, outcome_at, claim_id, idempotency_key):
                        delivered += 1
                else:
                    if self._record_failure(occurrence, outcome, outcome_at, claim_id, idempotency_key):
                        failed += 1
            except Exception:
                self._restore_claim(occurrence.id, outcome_at, claim_id)
                raise
        return TickResult(created, attempted, delivered, failed)

    def handle_reply(self, text: str, identity: ReplyIdentity) -> ReplyResult:
        try:
            parsed = parse_reply(text)
        except ValidationError:
            return ReplyResult(False, reason="invalid")
        if parsed is None:
            return ReplyResult(False, reason="unrelated")
        if not identity.is_dm:
            return ReplyResult(False, reason="group-traffic")
        with self.db.transaction() as connection:
            matches = self._resolve_open_occurrences(connection, identity)
            if len(matches) != 1:
                return ReplyResult(False, parsed.command, reason="ambiguous" if matches else "not-found")
            occurrence_id, reminder_id = matches[0]
            if parsed.command == ReplyCommand.DONE:
                _set_occurrence(connection, occurrence_id, OccurrenceStatus.DONE, self.clock.now())
                _insert_event(connection, Event(None, "occurrence", occurrence_id, "occurrence.done", {"reply": parsed.command.value}, self.clock.now()))
            elif parsed.command == ReplyCommand.SNOOZE:
                reminder = self.db.get_reminder(reminder_id)
                assert reminder is not None
                minutes = parsed.snooze_minutes or reminder.default_snooze_minutes
                due_at = self.clock.now() + duration_delta(minutes)
                _set_occurrence(connection, occurrence_id, OccurrenceStatus.SNOOZED, self.clock.now(), due_at=due_at)
                _insert_event(connection, Event(None, "occurrence", occurrence_id, "occurrence.snoozed", {"minutes": minutes}, self.clock.now()))
            else:
                _set_reminder(connection, reminder_id, ReminderStatus.CANCELLED, self.clock.now())
                rows = connection.execute(
                    "SELECT id FROM occurrences WHERE reminder_id = ? AND status IN (?, ?, ?, ?)",
                    (
                        reminder_id,
                        OccurrenceStatus.DUE.value,
                        OccurrenceStatus.DELIVERED.value,
                        OccurrenceStatus.SNOOZED.value,
                        OccurrenceStatus.DELIVERING.value,
                    ),
                ).fetchall()
                for row in rows:
                    _set_occurrence(connection, row["id"], OccurrenceStatus.CANCELLED, self.clock.now())
                    _insert_event(connection, Event(None, "occurrence", row["id"], "occurrence.cancelled", {"reply": "cancel"}, self.clock.now()))
                _insert_event(connection, Event(None, "reminder", reminder_id, "reminder.cancelled", {"reply": "cancel"}, self.clock.now()))
        return ReplyResult(True, parsed.command, occurrence_id)

    def _create_due_occurrences(self, now: datetime) -> int:
        created = 0
        rows = self.db.connection.execute("SELECT * FROM reminders WHERE status = ?", (ReminderStatus.ACTIVE.value,)).fetchall()
        for row in rows:
            reminder = self.db.get_reminder(row["id"])
            if reminder is None:
                continue
            last = self.db.connection.execute(
                "SELECT MAX(scheduled_for) FROM occurrences WHERE reminder_id = ?", (reminder.id,)
            ).fetchone()[0]
            start = datetime_from_iso(last) + timedelta(microseconds=1) if last else reminder.created_at
            end = now + timedelta(microseconds=1)
            if start >= end:
                continue
            for due in due_times_between(_public_schedule(reminder.schedule), reminder.timezone, start, end):
                occurrence = Occurrence(_occurrence_id(reminder.id, due), reminder.id, due, due_at=due, created_at=now, updated_at=now)
                try:
                    self.db.add_occurrence(occurrence)
                    created += 1
                except sqlite3.IntegrityError:
                    pass
        self._queue_escalations(now)
        return created

    def _queue_escalations(self, now: datetime) -> None:
        rows = self.db.connection.execute("SELECT id FROM occurrences WHERE status = ?", (OccurrenceStatus.DELIVERED.value,)).fetchall()
        for row in rows:
            try:
                with self.db.transaction() as connection:
                    locked = connection.execute(
                        """
                        SELECT o.*, r.schedule_json, r.escalation_minutes_json
                        FROM occurrences o
                        JOIN reminders r ON r.id = o.reminder_id
                        WHERE o.id = ?
                          AND o.status = ?
                          AND r.status = ?
                        """,
                        (row["id"], OccurrenceStatus.DELIVERED.value, ReminderStatus.ACTIVE.value),
                    ).fetchone()
                    if locked is None:
                        continue
                    deliveries = _success_count(connection, locked["id"])
                    schedule = json.loads(locked["schedule_json"])
                    max_deliveries = _meta(schedule)["max_deliveries"]
                    if deliveries >= max_deliveries:
                        continue
                    intervals = tuple(json.loads(locked["escalation_minutes_json"]))
                    next_due = _next_escalation_due(connection, intervals, schedule, locked["id"], deliveries)
                    if next_due is None or next_due > now:
                        continue
                    cursor = connection.execute(
                        """
                        UPDATE occurrences
                        SET status = ?, due_at = ?, updated_at = ?, delivery_claim_id = NULL
                        WHERE id = ?
                          AND status = ?
                          AND reminder_id IN (SELECT id FROM reminders WHERE id = ? AND status = ?)
                          AND (SELECT COUNT(DISTINCT logical_delivery_id) FROM delivery_attempts WHERE occurrence_id = ? AND status = ? AND applied_to_occurrence = 1) = ?
                        """,
                        (
                            OccurrenceStatus.DUE.value,
                            datetime_to_iso(now),
                            datetime_to_iso(now),
                            locked["id"],
                            OccurrenceStatus.DELIVERED.value,
                            locked["reminder_id"],
                            ReminderStatus.ACTIVE.value,
                            locked["id"],
                            DeliveryStatus.SUCCESS.value,
                            deliveries,
                        ),
                    )
                    if cursor.rowcount == 1:
                        _insert_event(connection, Event(None, "occurrence", locked["id"], "occurrence.escalated", {"delivery_number": deliveries + 1}, now))
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc):
                    raise
                continue

    def _transport_due(self, occurrence_id: str, now: datetime) -> bool:
        failure_rows = self.db.connection.execute(
            "SELECT attempted_at, status, applied_to_occurrence FROM delivery_attempts WHERE occurrence_id = ? ORDER BY attempted_at DESC, id DESC",
            (occurrence_id,),
        ).fetchall()
        failures = 0
        last_failure: datetime | None = None
        for row in failure_rows:
            if row["status"] == DeliveryStatus.SUCCESS.value and row["applied_to_occurrence"]:
                break
            failures += 1
            last_failure = last_failure or datetime_from_iso(row["attempted_at"])
        if failures == 0 or last_failure is None:
            return True
        delay = _RETRY_MINUTES[failures - 1] if failures <= len(_RETRY_MINUTES) else _MAX_RETRY_MINUTES
        return now >= last_failure + timedelta(minutes=delay)

    def _recover_stale_claims(self, now: datetime) -> None:
        stale_before = now - timedelta(minutes=_DELIVERY_CLAIM_LEASE_MINUTES)
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM occurrences WHERE status = ? AND updated_at <= ?",
                (OccurrenceStatus.DELIVERING.value, datetime_to_iso(stale_before)),
            ).fetchall()
            for row in rows:
                if _set_occurrence_if_status(connection, row["id"], OccurrenceStatus.DUE, now, OccurrenceStatus.DELIVERING):
                    _insert_event(connection, Event(None, "occurrence", row["id"], "delivery.claim.expired", {}, now))

    def _restore_claim(self, occurrence_id: str, now: datetime, claim_id: str) -> None:
        try:
            with self.db.transaction() as connection:
                if _set_occurrence_if_status(connection, occurrence_id, OccurrenceStatus.DUE, now, OccurrenceStatus.DELIVERING, claim_id=claim_id):
                    _insert_event(connection, Event(None, "occurrence", occurrence_id, "delivery.claim.restored", {}, now))
        except Exception:
            pass

    def _claim_occurrence(self, occurrence_id: str, now: datetime) -> str | None:
        claim_id = uuid.uuid4().hex
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE occurrences SET status = ?, updated_at = ?, delivery_claim_id = ? WHERE id = ? AND status IN (?, ?)",
                (
                    OccurrenceStatus.DELIVERING.value,
                    datetime_to_iso(now),
                    claim_id,
                    occurrence_id,
                    OccurrenceStatus.DUE.value,
                    OccurrenceStatus.SNOOZED.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            _insert_event(connection, Event(None, "occurrence", occurrence_id, "delivery.claimed", {"claim_id": claim_id}, now))
        return claim_id

    def _delivery_idempotency_key(self, occurrence_id: str) -> str:
        latest = self.db.connection.execute(
            """
            SELECT logical_delivery_id
            FROM delivery_attempts
            WHERE occurrence_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (occurrence_id,),
        ).fetchone()
        if latest is not None:
            applied_for_latest = self.db.connection.execute(
                """
                SELECT 1
                FROM delivery_attempts
                WHERE occurrence_id = ?
                  AND logical_delivery_id = ?
                  AND status = ?
                  AND applied_to_occurrence = 1
                LIMIT 1
                """,
                (occurrence_id, latest["logical_delivery_id"], DeliveryStatus.SUCCESS.value),
            ).fetchone()
            if applied_for_latest is None:
                return str(latest["logical_delivery_id"])
        return f"replyloop:{occurrence_id}:delivery:{_success_count(self.db.connection, occurrence_id) + 1}"

    def _record_failure(self, occurrence: Occurrence, outcome: DeliveryOutcome, now: datetime, claim_id: str, logical_delivery_id: str) -> bool:
        attempt = DeliveryAttempt(
            _attempt_id(occurrence.id, now, "failure", claim_id),
            occurrence.id,
            logical_delivery_id,
            now,
            DeliveryStatus.FAILURE,
            outcome.transport,
            outcome.error,
            False,
            now,
        )
        with self.db.transaction() as connection:
            applied = _set_occurrence_if_status(connection, occurrence.id, OccurrenceStatus.DUE, now, OccurrenceStatus.DELIVERING, claim_id=claim_id)
            connection.execute(
                """
                INSERT INTO delivery_attempts(
                    id, occurrence_id, logical_delivery_id, attempted_at, status,
                    transport, error, applied_to_occurrence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.id,
                    attempt.occurrence_id,
                    attempt.logical_delivery_id,
                    datetime_to_iso(attempt.attempted_at),
                    attempt.status.value,
                    attempt.transport,
                    attempt.error,
                    int(applied),
                    datetime_to_iso(attempt.created_at),
                ),
            )
            _insert_event(connection, Event(None, "occurrence", occurrence.id, "delivery.failed", {"attempt_id": attempt.id, "status": attempt.status.value, "transport": attempt.transport, "logical_delivery_id": logical_delivery_id, "applied": applied}, now))
        return applied

    def _record_success(self, reminder: Reminder, occurrence: Occurrence, outcome: DeliveryOutcome, now: datetime, claim_id: str, logical_delivery_id: str) -> bool:
        attempt_id = _attempt_id(occurrence.id, now, "success", claim_id)
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE occurrences
                SET status = ?, updated_at = ?, delivery_claim_id = NULL
                WHERE id = ?
                  AND status = ?
                  AND delivery_claim_id = ?
                  AND EXISTS (SELECT 1 FROM reminders WHERE id = ? AND status IN (?, ?))
                """,
                (
                    OccurrenceStatus.DELIVERED.value,
                    datetime_to_iso(now),
                    occurrence.id,
                    OccurrenceStatus.DELIVERING.value,
                    claim_id,
                    reminder.id,
                    ReminderStatus.ACTIVE.value,
                    ReminderStatus.PAUSED.value,
                ),
            )
            applied = cursor.rowcount == 1
            attempt = DeliveryAttempt(attempt_id, occurrence.id, logical_delivery_id, now, DeliveryStatus.SUCCESS, outcome.transport, applied_to_occurrence=applied, created_at=now)
            connection.execute(
                """
                INSERT INTO delivery_attempts(
                    id, occurrence_id, logical_delivery_id, attempted_at, status,
                    transport, error, applied_to_occurrence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt.id,
                    attempt.occurrence_id,
                    attempt.logical_delivery_id,
                    datetime_to_iso(attempt.attempted_at),
                    attempt.status.value,
                    attempt.transport,
                    None,
                    int(attempt.applied_to_occurrence),
                    datetime_to_iso(attempt.created_at),
                ),
            )
            _insert_event(connection, Event(None, "occurrence", occurrence.id, "delivery.succeeded", {"attempt_id": attempt.id, "transport": outcome.transport, "provider_message_id": outcome.provider_message_id, "logical_delivery_id": logical_delivery_id, "delivery_number": _success_count(connection, occurrence.id), "applied": applied}, now))
        return applied

    def _resolve_open_occurrences(self, connection: sqlite3.Connection, identity: ReplyIdentity) -> list[tuple[str, str]]:
        rows = connection.execute(
            """
            SELECT o.id, o.reminder_id, MAX(a.attempted_at) AS delivered_at, r.target
            FROM occurrences o
            JOIN reminders r ON r.id = o.reminder_id
            JOIN delivery_attempts a ON a.occurrence_id = o.id AND a.status = ?
            WHERE o.status IN (?, ?, ?, ?)
              AND r.status = ?
            GROUP BY o.id, o.reminder_id, r.target
            ORDER BY delivered_at DESC, o.id DESC
            """,
            (
                DeliveryStatus.SUCCESS.value,
                OccurrenceStatus.DELIVERED.value,
                OccurrenceStatus.SNOOZED.value,
                OccurrenceStatus.DUE.value,
                OccurrenceStatus.DELIVERING.value,
                ReminderStatus.ACTIVE.value,
            ),
        ).fetchall()
        matches: list[tuple[str, str]] = []
        latest_updated_at: str | None = None
        for row in rows:
            if target_matches(_decode_target(row["target"]), identity):
                if latest_updated_at is None:
                    latest_updated_at = row["delivered_at"]
                elif row["delivered_at"] != latest_updated_at:
                    break
                matches.append((row["id"], row["reminder_id"]))
        return matches


def _validate_target(target: dict[str, Any]) -> None:
    if not isinstance(target, dict):
        raise ValidationError("target must be a mapping")
    for key in ("platform", "chat_id"):
        if not isinstance(target.get(key), str) or not target[key]:
            raise ValidationError(f"target {key} is required")
    if "sender_id" in target and target["sender_id"] is not None and not isinstance(target["sender_id"], str):
        raise ValidationError("target sender_id must be a string")
    if "is_dm" in target and not isinstance(target["is_dm"], bool):
        raise ValidationError("target is_dm must be a boolean")


def _validate_duration_minutes(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    if value <= 0 or value > _MAX_REPLY_DURATION_MINUTES:
        raise ValidationError(f"{name} is out of bounds")


def _validate_intervals(intervals: tuple[int, ...], max_deliveries: int) -> None:
    if not isinstance(max_deliveries, int) or isinstance(max_deliveries, bool):
        raise ValidationError("max_deliveries must be an integer")
    if max_deliveries <= 0:
        raise ValidationError("max_deliveries must be positive")
    if not isinstance(intervals, tuple):
        raise ValidationError("intervals_minutes must be a tuple")
    for value in intervals:
        _validate_duration_minutes(value, "intervals_minutes")
    if max_deliveries > 1 and not intervals:
        raise ValidationError("intervals_minutes are required for escalation")


def _decode_target(value: str) -> dict[str, Any]:
    try:
        target = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError("stored target is not JSON") from exc
    _validate_target(target)
    return target


def _public_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in schedule.items() if key != "_replyloop"}


def _meta(schedule: dict[str, Any]) -> dict[str, Any]:
    meta = schedule.get("_replyloop", {})
    return {"max_deliveries": int(meta.get("max_deliveries", 1)), "repeat_last": bool(meta.get("repeat_last", False))}


def _message_for(reminder: Reminder, occurrence: Occurrence) -> str:
    return f"Reminder {reminder.id} due at {datetime_to_iso(occurrence.scheduled_for)}. Reply DONE, SNOOZE, or CANCEL."


def _occurrence_id(reminder_id: str, scheduled_for: datetime) -> str:
    digest = hashlib.sha256(f"{reminder_id}|{datetime_to_iso(scheduled_for)}".encode()).hexdigest()[:16]
    return f"occ_{digest}"


def _attempt_id(occurrence_id: str, attempted_at: datetime, status: str, claim_id: str) -> str:
    digest = hashlib.sha256(f"{occurrence_id}|{datetime_to_iso(attempted_at)}|{status}|{claim_id}".encode()).hexdigest()[:16]
    return f"att_{digest}"


def _success_count(connection: sqlite3.Connection, occurrence_id: str) -> int:
    return int(connection.execute("SELECT COUNT(DISTINCT logical_delivery_id) FROM delivery_attempts WHERE occurrence_id = ? AND status = ? AND applied_to_occurrence = 1", (occurrence_id, DeliveryStatus.SUCCESS.value)).fetchone()[0])


def _next_escalation_due(connection: sqlite3.Connection, intervals: tuple[int, ...], schedule: dict[str, Any], occurrence_id: str, deliveries: int) -> datetime | None:
    if deliveries <= 0:
        return None
    if not intervals:
        return None
    index = deliveries - 1
    meta = _meta(schedule)
    if index >= len(intervals):
        if not meta["repeat_last"]:
            return None
        interval = intervals[-1]
    else:
        interval = intervals[index]
    row = connection.execute("SELECT MAX(attempted_at) FROM delivery_attempts WHERE occurrence_id = ? AND status = ? AND applied_to_occurrence = 1", (occurrence_id, DeliveryStatus.SUCCESS.value)).fetchone()
    if row[0] is None:
        return None
    return datetime_from_iso(row[0]) + timedelta(minutes=interval)


def _set_occurrence(connection: sqlite3.Connection, occurrence_id: str, status: OccurrenceStatus, now: datetime, *, due_at: datetime | None = None) -> None:
    due = ", due_at = ?" if due_at is not None else ""
    params: tuple[Any, ...]
    if due_at is not None:
        params = (status.value, datetime_to_iso(to_utc(due_at)), datetime_to_iso(now), occurrence_id)
    else:
        params = (status.value, datetime_to_iso(now), occurrence_id)
    cursor = connection.execute(f"UPDATE occurrences SET status = ?{due}, updated_at = ? WHERE id = ?", params)
    if cursor.rowcount != 1:
        raise KeyError(occurrence_id)


def _set_occurrence_if_status(
    connection: sqlite3.Connection,
    occurrence_id: str,
    status: OccurrenceStatus,
    now: datetime,
    expected_status: OccurrenceStatus,
    *,
    claim_id: str | None = None,
) -> bool:
    claim_filter = " AND delivery_claim_id = ?" if claim_id is not None else ""
    params: tuple[Any, ...]
    if claim_id is not None:
        params = (status.value, datetime_to_iso(now), occurrence_id, expected_status.value, claim_id)
    else:
        params = (status.value, datetime_to_iso(now), occurrence_id, expected_status.value)
    cursor = connection.execute(
        f"UPDATE occurrences SET status = ?, updated_at = ?, delivery_claim_id = NULL WHERE id = ? AND status = ?{claim_filter}",
        params,
    )
    return cursor.rowcount == 1


def _set_reminder(connection: sqlite3.Connection, reminder_id: str, status: ReminderStatus, now: datetime) -> None:
    cursor = connection.execute("UPDATE reminders SET status = ?, updated_at = ? WHERE id = ?", (status.value, datetime_to_iso(now), reminder_id))
    if cursor.rowcount != 1:
        raise KeyError(reminder_id)


def _insert_event(connection: sqlite3.Connection, event: Event) -> None:
    connection.execute(
        "INSERT INTO events(aggregate_type, aggregate_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (event.aggregate_type, event.aggregate_id, event.event_type, json.dumps(event.payload, sort_keys=True, separators=(",", ":")), datetime_to_iso(event.created_at)),
    )
