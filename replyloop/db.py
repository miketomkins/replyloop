"""SQLite storage and migrations for ReplyLoop."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import MigrationError
from .models import (
    DeliveryAttempt,
    DeliveryStatus,
    Event,
    Occurrence,
    OccurrenceStatus,
    Reminder,
    ReminderStatus,
    datetime_from_iso,
    datetime_to_iso,
    utc_now,
)
from .schedules import validate_schedule

MIGRATIONS_PACKAGE = "replyloop.migrations"


class ReplyLoopDB:
    """Small transactional SQLite repository.

    Mutating helpers that also append an event do both writes in one transaction
    through :meth:`mutate_with_event`, so current-state projections and the
    append-only history cannot diverge on rollback.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path, timeout=30.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        try:
            self.connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc):
                raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> ReplyLoopDB:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def migrate(self) -> None:
        with self.transaction():
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                row["version"]
                for row in self.connection.execute("SELECT version FROM schema_migrations ORDER BY version")
            }
            for version, sql in _load_migrations():
                if version in applied:
                    continue
                for statement in _split_sql_statements(sql):
                    self.connection.execute(statement)
                self.connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime_to_iso(utc_now())),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN")
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def mutate_with_event(self, mutation: Callable[[sqlite3.Connection], None], event: Event) -> Event:
        with self.transaction() as connection:
            mutation(connection)
            event_id = _insert_event(connection, event)
        return Event(
            id=event_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
        )

    def add_reminder(self, reminder: Reminder, event_type: str = "reminder.created") -> Event:
        validate_schedule({key: value for key, value in reminder.schedule.items() if key != "_replyloop"}, reminder.timezone)

        def mutation(connection: sqlite3.Connection) -> None:
            _insert_reminder(connection, reminder)

        return self.mutate_with_event(
            mutation,
            Event(None, "reminder", reminder.id, event_type, {"status": reminder.status.value}),
        )

    def update_reminder_status(self, reminder_id: str, status: ReminderStatus, event_type: str) -> Event:
        now = datetime_to_iso(utc_now())

        def mutation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, reminder_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(reminder_id)

        return self.mutate_with_event(
            mutation,
            Event(None, "reminder", reminder_id, event_type, {"status": status.value}),
        )

    def add_occurrence(self, occurrence: Occurrence, event_type: str = "occurrence.created") -> Event:
        def mutation(connection: sqlite3.Connection) -> None:
            _insert_occurrence(connection, occurrence)

        return self.mutate_with_event(
            mutation,
            Event(
                None,
                "occurrence",
                occurrence.id,
                event_type,
                {"reminder_id": occurrence.reminder_id, "scheduled_for": datetime_to_iso(occurrence.scheduled_for)},
            ),
        )

    def update_occurrence_status(self, occurrence_id: str, status: OccurrenceStatus, event_type: str) -> Event:
        now = datetime_to_iso(utc_now())

        def mutation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                "UPDATE occurrences SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, now, occurrence_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(occurrence_id)

        return self.mutate_with_event(
            mutation,
            Event(None, "occurrence", occurrence_id, event_type, {"status": status.value}),
        )

    def add_delivery_attempt(self, attempt: DeliveryAttempt, event_type: str = "delivery.attempted") -> Event:
        def mutation(connection: sqlite3.Connection) -> None:
            _insert_delivery_attempt(connection, attempt)

        return self.mutate_with_event(
            mutation,
            Event(
                None,
                "occurrence",
                attempt.occurrence_id,
                event_type,
                {
                    "attempt_id": attempt.id,
                    "status": attempt.status.value,
                    "transport": attempt.transport,
                    "logical_delivery_id": attempt.logical_delivery_id,
                    "applied": attempt.applied_to_occurrence,
                },
            ),
        )

    def append_event(self, event: Event) -> Event:
        with self.transaction() as connection:
            event_id = _insert_event(connection, event)
        return Event(event_id, event.aggregate_type, event.aggregate_id, event.event_type, event.payload, event.created_at)

    def get_reminder(self, reminder_id: str) -> Reminder | None:
        row = self.connection.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return _row_to_reminder(row) if row is not None else None

    def get_occurrence(self, occurrence_id: str) -> Occurrence | None:
        row = self.connection.execute("SELECT * FROM occurrences WHERE id = ?", (occurrence_id,)).fetchone()
        return _row_to_occurrence(row) if row is not None else None

    def list_due_occurrences(self, before_utc: datetime) -> list[Occurrence]:
        rows = self.connection.execute(
            "SELECT * FROM occurrences WHERE status IN (?, ?) AND due_at < ? ORDER BY due_at, id",
            (OccurrenceStatus.DUE.value, OccurrenceStatus.SNOOZED.value, datetime_to_iso(before_utc)),
        ).fetchall()
        return [_row_to_occurrence(row) for row in rows]

    def count_due_occurrences(self, before_utc: datetime) -> int:
        return len(self.list_due_occurrences(before_utc))

    def list_events(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> list[Event]:
        if aggregate_type is None and aggregate_id is None:
            rows = self.connection.execute("SELECT * FROM events ORDER BY id").fetchall()
        elif aggregate_type is not None and aggregate_id is not None:
            rows = self.connection.execute(
                "SELECT * FROM events WHERE aggregate_type = ? AND aggregate_id = ? ORDER BY id",
                (aggregate_type, aggregate_id),
            ).fetchall()
        else:
            raise ValueError("aggregate_type and aggregate_id must be provided together")
        return [_row_to_event(row) for row in rows]


def connect(path: str | Path) -> ReplyLoopDB:
    db = ReplyLoopDB(path)
    for attempt in range(5):
        try:
            db.migrate()
            break
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc) or attempt == 4:
                db.close()
                raise
            time.sleep(0.05 * (attempt + 1))
    return db


def _load_migrations() -> list[tuple[str, str]]:
    try:
        root = resources.files(MIGRATIONS_PACKAGE)
        migrations = []
        for item in sorted(root.iterdir(), key=lambda value: value.name):
            if item.name.endswith(".sql"):
                migrations.append((item.name.removesuffix(".sql"), item.read_text(encoding="utf-8")))
        return migrations
    except Exception as exc:
        raise MigrationError("failed to load migrations") from exc


def _split_sql_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def _insert_reminder(connection: sqlite3.Connection, reminder: Reminder) -> None:
    connection.execute(
        """
        INSERT INTO reminders(
            id, target, schedule_json, timezone, status, default_snooze_minutes,
            escalation_minutes_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reminder.id,
            reminder.target,
            json.dumps(reminder.schedule, sort_keys=True, separators=(",", ":")),
            reminder.timezone,
            reminder.status.value,
            reminder.default_snooze_minutes,
            json.dumps(list(reminder.escalation_minutes), separators=(",", ":")),
            datetime_to_iso(reminder.created_at),
            datetime_to_iso(reminder.updated_at),
        ),
    )


def _insert_occurrence(connection: sqlite3.Connection, occurrence: Occurrence) -> None:
    due_at = occurrence.due_at or occurrence.scheduled_for
    connection.execute(
        """
        INSERT INTO occurrences(id, reminder_id, scheduled_for, due_at, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            occurrence.id,
            occurrence.reminder_id,
            datetime_to_iso(occurrence.scheduled_for),
            datetime_to_iso(due_at),
            occurrence.status.value,
            datetime_to_iso(occurrence.created_at),
            datetime_to_iso(occurrence.updated_at),
        ),
    )


def _insert_delivery_attempt(connection: sqlite3.Connection, attempt: DeliveryAttempt) -> None:
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
            int(attempt.applied_to_occurrence),
            datetime_to_iso(attempt.created_at),
        ),
    )


def _insert_event(connection: sqlite3.Connection, event: Event) -> int:
    cursor = connection.execute(
        """
        INSERT INTO events(aggregate_type, aggregate_id, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            event.aggregate_type,
            event.aggregate_id,
            event.event_type,
            json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
            datetime_to_iso(event.created_at),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("event insert did not return an id")
    return int(cursor.lastrowid)


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=row["id"],
        target=row["target"],
        schedule=json.loads(row["schedule_json"]),
        timezone=row["timezone"],
        status=ReminderStatus(row["status"]),
        default_snooze_minutes=row["default_snooze_minutes"],
        escalation_minutes=tuple(json.loads(row["escalation_minutes_json"])),
        created_at=datetime_from_iso(row["created_at"]),
        updated_at=datetime_from_iso(row["updated_at"]),
    )


def _row_to_occurrence(row: sqlite3.Row) -> Occurrence:
    return Occurrence(
        id=row["id"],
        reminder_id=row["reminder_id"],
        scheduled_for=datetime_from_iso(row["scheduled_for"]),
        due_at=datetime_from_iso(row["due_at"]),
        status=OccurrenceStatus(row["status"]),
        created_at=datetime_from_iso(row["created_at"]),
        updated_at=datetime_from_iso(row["updated_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        event_type=row["event_type"],
        payload=json.loads(row["payload_json"]),
        created_at=datetime_from_iso(row["created_at"]),
    )


def delivery_attempt_from_row(row: sqlite3.Row) -> DeliveryAttempt:
    return DeliveryAttempt(
        id=row["id"],
        occurrence_id=row["occurrence_id"],
        logical_delivery_id=row["logical_delivery_id"],
        attempted_at=datetime_from_iso(row["attempted_at"]),
        status=DeliveryStatus(row["status"]),
        transport=row["transport"],
        error=row["error"],
        applied_to_occurrence=bool(row["applied_to_occurrence"]),
        created_at=datetime_from_iso(row["created_at"]),
    )
