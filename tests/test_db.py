from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from replyloop.db import connect, delivery_attempt_from_row
from replyloop.errors import ValidationError
from replyloop.models import DeliveryStatus, Occurrence, OccurrenceStatus, Reminder, ReminderStatus

UTC = timezone.utc


def sample_reminder(identifier: str = "reminder-alpha") -> Reminder:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return Reminder(
        id=identifier,
        target="synthetic-direct-target",
        title="Check in",
        message="Send the update.",
        schedule={"kind": "daily", "times": ["09:00"]},
        timezone="UTC",
        default_snooze_minutes=45,
        escalation_minutes=(30, 90),
        created_at=now,
        updated_at=now,
    )


def sample_occurrence(identifier: str = "occurrence-alpha") -> Occurrence:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    return Occurrence(id=identifier, reminder_id="reminder-alpha", scheduled_for=now, due_at=now)


class DatabaseTests(unittest.TestCase):
    def test_reminder_rejects_non_string_blank_and_whitespace_content(self) -> None:
        invalid_values = (None, 123, "", "   ", "\n\t")
        for field in ("title", "message"):
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    kwargs = {
                        "id": "reminder-invalid-content",
                        "target": "synthetic-direct-target",
                        "title": "Valid title",
                        "message": "Valid message",
                        "schedule": {"kind": "daily", "times": ["09:00"]},
                        "timezone": "UTC",
                    }
                    kwargs[field] = value
                    with self.assertRaises(ValidationError):
                        Reminder(**kwargs)

    def test_reminder_normalizes_edge_whitespace_once_before_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            reminder = Reminder(
                id="reminder-normalized",
                target="synthetic-direct-target",
                title=" \tEdge title\n ",
                message="\n Edge message\t ",
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
            )
            db.add_reminder(reminder)

            stored = db.get_reminder("reminder-normalized")
            raw = db.connection.execute("SELECT title, message FROM reminders WHERE id = ?", ("reminder-normalized",)).fetchone()
            db.close()

        assert stored is not None
        self.assertEqual(reminder.title, "Edge title")
        self.assertEqual(reminder.message, "Edge message")
        self.assertEqual(stored.title, "Edge title")
        self.assertEqual(stored.message, "Edge message")
        self.assertEqual(raw["title"], "Edge title")
        self.assertEqual(raw["message"], "Edge message")

    def test_reminder_malformed_timezone_key_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            Reminder(id="reminder-bad-zone", target="synthetic-direct-target", title="Bad zone", message="Should fail", schedule={"kind": "daily", "times": ["09:00"]}, timezone="../UTC")

    def test_migration_is_idempotent_and_sets_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            db.migrate()
            versions = [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations")]
            foreign_keys = db.connection.execute("PRAGMA foreign_keys").fetchone()[0]
            journal_mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
            db.close()

        self.assertEqual(versions, ["001_initial", "002_delivery_claim_ids", "003_logical_delivery_identity", "004_reminder_content_and_receipts"])
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(journal_mode, "wal")

    def test_populated_v003_migration_preserves_rows_and_materializes_historical_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            self._create_populated_v003_database(path)

            db = connect(path)
            versions = [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            reminder = db.get_reminder("legacy-reminder")
            occurrence = db.get_occurrence("legacy-occurrence")
            events = db.list_events()
            raw_attempts = db.connection.execute("SELECT * FROM delivery_attempts ORDER BY attempted_at, id").fetchall()
            materialized_attempts = [delivery_attempt_from_row(row) for row in raw_attempts]
            due = db.list_due_occurrences(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
            db.close()

            reopened = connect(path)
            reopened_versions = [row[0] for row in reopened.connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            reopened_attempt_count = reopened.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0]
            reopened_event_count = reopened.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            reopened.close()

        assert reminder is not None
        assert occurrence is not None
        self.assertEqual(versions, ["001_initial", "002_delivery_claim_ids", "003_logical_delivery_identity", "004_reminder_content_and_receipts"])
        self.assertEqual(reopened_versions, versions)
        self.assertEqual(reminder.title, "Reminder legacy-reminder")
        self.assertEqual(reminder.message, "Reminder legacy-reminder is due.")
        self.assertEqual(occurrence.status, OccurrenceStatus.DUE)
        self.assertEqual([event.event_type for event in events], ["reminder.created", "occurrence.created", "delivery.succeeded"])
        self.assertEqual([attempt.status for attempt in materialized_attempts], [DeliveryStatus.SUCCESS])
        self.assertIsNone(materialized_attempts[0].provider_message_id)
        self.assertTrue(materialized_attempts[0].applied_to_occurrence)
        self.assertEqual([item.id for item in due], ["legacy-occurrence"])
        self.assertEqual(reopened_attempt_count, 1)
        self.assertEqual(reopened_event_count, 3)

    def test_add_reminder_round_trips_with_timezone_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            reminder = sample_reminder()
            event = db.add_reminder(reminder)

            loaded = db.get_reminder(reminder.id)
            events = db.list_events("reminder", reminder.id)
            db.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.timezone, "UTC")
        self.assertEqual(loaded.title, "Check in")
        self.assertEqual(loaded.message, "Send the update.")
        self.assertEqual(loaded.schedule, {"kind": "daily", "times": ["09:00"]})
        self.assertEqual(event.id, 1)
        self.assertEqual([item.event_type for item in events], ["reminder.created"])

    def test_occurrence_unique_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            db.add_reminder(sample_reminder())
            db.add_occurrence(sample_occurrence("occurrence-one"))
            with self.assertRaises(sqlite3.IntegrityError):
                db.add_occurrence(sample_occurrence("occurrence-two"))
            db.close()

    def test_transaction_rollback_keeps_projection_and_events_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            db.add_reminder(sample_reminder())
            before = db.list_events()

            with self.assertRaises(KeyError):
                db.update_reminder_status("missing-reminder", ReminderStatus.CANCELLED, "reminder.cancelled")

            loaded = db.get_reminder("reminder-alpha")
            after = db.list_events()
            db.close()

        assert loaded is not None
        self.assertEqual(loaded.status, ReminderStatus.ACTIVE)
        self.assertEqual([event.event_type for event in before], ["reminder.created"])
        self.assertEqual([event.event_type for event in after], ["reminder.created"])

    def test_reopen_restart_preserves_due_query_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            db.add_reminder(sample_reminder())
            db.add_occurrence(sample_occurrence())
            db.close()

            reopened = connect(path)
            due = reopened.list_due_occurrences(datetime(2026, 1, 2, 0, 0, tzinfo=UTC))
            reopened.update_occurrence_status("occurrence-alpha", OccurrenceStatus.DONE, "occurrence.done")
            done = reopened.get_occurrence("occurrence-alpha")
            reopened.close()

        self.assertEqual([item.id for item in due], ["occurrence-alpha"])
        assert done is not None
        self.assertEqual(done.status, OccurrenceStatus.DONE)

    def test_due_query_includes_whole_second_before_fractional_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            db.add_reminder(sample_reminder())
            db.add_occurrence(sample_occurrence("occurrence-whole-second"))

            due = db.list_due_occurrences(datetime(2026, 1, 1, 9, 0, 0, 500000, tzinfo=UTC))
            db.close()

        self.assertEqual([item.id for item in due], ["occurrence-whole-second"])

    def test_due_query_orders_fractional_second_after_whole_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            db.add_reminder(sample_reminder())
            whole_second = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
            fractional_second = datetime(2026, 1, 1, 9, 0, 0, 500000, tzinfo=UTC)
            db.add_occurrence(Occurrence("occurrence-fractional", "reminder-alpha", fractional_second, due_at=fractional_second))
            db.add_occurrence(Occurrence("occurrence-whole", "reminder-alpha", whole_second, due_at=whole_second))

            due = db.list_due_occurrences(datetime(2026, 1, 1, 9, 0, 1, tzinfo=UTC))
            db.close()

        self.assertEqual([item.id for item in due], ["occurrence-whole", "occurrence-fractional"])

    def _create_populated_v003_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            for version in ("001_initial", "002_delivery_claim_ids", "003_logical_delivery_identity"):
                sql = resources.files("replyloop.migrations").joinpath(f"{version}.sql").read_text(encoding="utf-8")
                connection.executescript(sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, "2026-01-01T00:00:00.000000Z"),
                )
            connection.execute(
                """
                INSERT INTO reminders(
                    id, target, schedule_json, timezone, status, default_snooze_minutes,
                    escalation_minutes_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-reminder",
                    "synthetic-direct-target",
                    '{"kind":"daily","times":["09:00"]}',
                    "UTC",
                    "active",
                    60,
                    "[]",
                    "2026-01-01T00:00:00.000000Z",
                    "2026-01-01T00:00:00.000000Z",
                ),
            )
            connection.execute(
                """
                INSERT INTO occurrences(
                    id, reminder_id, scheduled_for, due_at, status, created_at, updated_at,
                    delivery_claim_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-occurrence",
                    "legacy-reminder",
                    "2026-01-01T09:00:00.000000Z",
                    "2026-01-01T09:00:00.000000Z",
                    "due",
                    "2026-01-01T00:00:00.000000Z",
                    "2026-01-01T00:00:00.000000Z",
                    None,
                ),
            )
            connection.execute(
                """
                INSERT INTO delivery_attempts(
                    id, occurrence_id, attempted_at, status, transport, error, created_at,
                    logical_delivery_id, applied_to_occurrence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-attempt-success",
                    "legacy-occurrence",
                    "2026-01-01T09:00:00.000000Z",
                    "success",
                    "synthetic",
                    None,
                    "2026-01-01T09:00:00.000000Z",
                    "replyloop:legacy-occurrence:delivery:1",
                    1,
                ),
            )
            for aggregate_type, aggregate_id, event_type, payload in (
                ("reminder", "legacy-reminder", "reminder.created", '{"status":"active"}'),
                ("occurrence", "legacy-occurrence", "occurrence.created", '{"reminder_id":"legacy-reminder"}'),
                ("occurrence", "legacy-occurrence", "delivery.succeeded", '{"attempt_id":"legacy-attempt-success"}'),
            ):
                connection.execute(
                    "INSERT INTO events(aggregate_type, aggregate_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (aggregate_type, aggregate_id, event_type, payload, "2026-01-01T00:00:00.000000Z"),
                )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
