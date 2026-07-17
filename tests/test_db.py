from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from replyloop.db import connect
from replyloop.errors import ValidationError
from replyloop.models import Occurrence, OccurrenceStatus, Reminder, ReminderStatus

UTC = timezone.utc


def sample_reminder(identifier: str = "reminder-alpha") -> Reminder:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return Reminder(
        id=identifier,
        target="synthetic-direct-target",
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
    def test_reminder_malformed_timezone_key_raises_validation_error(self) -> None:
        with self.assertRaises(ValidationError):
            Reminder(id="reminder-bad-zone", target="synthetic-direct-target", schedule={"kind": "daily", "times": ["09:00"]}, timezone="../UTC")

    def test_migration_is_idempotent_and_sets_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            db.migrate()
            versions = [row[0] for row in db.connection.execute("SELECT version FROM schema_migrations")]
            foreign_keys = db.connection.execute("PRAGMA foreign_keys").fetchone()[0]
            journal_mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
            db.close()

        self.assertEqual(versions, ["001_initial"])
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(journal_mode, "wal")

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


if __name__ == "__main__":
    unittest.main()
