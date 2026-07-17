from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from replyloop.db import connect
from replyloop.events import append_event, event_types, events_for
from replyloop.models import DeliveryAttempt, DeliveryStatus, Occurrence, Reminder

UTC = timezone.utc


def reminder() -> Reminder:
    now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return Reminder(
        id="reminder-events",
        target="synthetic-direct-target",
        schedule={"kind": "daily", "times": ["09:00"]},
        timezone="UTC",
        created_at=now,
        updated_at=now,
    )


def occurrence() -> Occurrence:
    now = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    return Occurrence(id="occurrence-events", reminder_id="reminder-events", scheduled_for=now, due_at=now)


class EventStoreTests(unittest.TestCase):
    def test_events_are_append_only_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            first = append_event(db, "reminder", "r-ordered", "one", {"n": 1})
            second = append_event(db, "reminder", "r-ordered", "two", {"n": 2})
            loaded = events_for(db, "reminder", "r-ordered")
            db.close()

        assert first.id is not None
        assert second.id is not None
        self.assertLess(first.id, second.id)
        self.assertEqual(event_types(loaded), ["one", "two"])
        self.assertEqual([event.payload["n"] for event in loaded], [1, 2])

    def test_delivery_attempt_and_event_share_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            db.add_reminder(reminder())
            db.add_occurrence(occurrence())
            attempt = DeliveryAttempt(
                id="attempt-one",
                occurrence_id="occurrence-events",
                logical_delivery_id="replyloop:occurrence-events:delivery:1",
                attempted_at=datetime(2026, 1, 1, 9, 1, tzinfo=UTC),
                status=DeliveryStatus.SUCCESS,
                transport="synthetic-transport",
                applied_to_occurrence=True,
            )
            event = db.add_delivery_attempt(attempt)
            events = events_for(db, "occurrence", "occurrence-events")
            attempts = db.connection.execute("SELECT id, status FROM delivery_attempts ORDER BY id").fetchall()
            db.close()

        self.assertEqual(event.event_type, "delivery.attempted")
        self.assertEqual([row["id"] for row in attempts], ["attempt-one"])
        self.assertEqual([item.event_type for item in events], ["occurrence.created", "delivery.attempted"])

    def test_failed_projection_mutation_does_not_append_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            db.add_reminder(reminder())
            with self.assertRaises(Exception):
                db.add_delivery_attempt(
                    DeliveryAttempt(
                        id="attempt-missing",
                        occurrence_id="missing-occurrence",
                        logical_delivery_id="replyloop:missing-occurrence:delivery:1",
                        attempted_at=datetime(2026, 1, 1, 9, 1, tzinfo=UTC),
                        status=DeliveryStatus.FAILURE,
                        transport="synthetic-transport",
                    )
                )
            events = db.list_events()
            attempts = db.connection.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0]
            db.close()

        self.assertEqual(attempts, 0)
        self.assertEqual(event_types(events), ["reminder.created"])


if __name__ == "__main__":
    unittest.main()
