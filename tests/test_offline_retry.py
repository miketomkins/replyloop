from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from replyloop.clock import FakeClock
from replyloop.db import connect
from replyloop.delivery import DeliveryOutcome, RecordingAdapter
from replyloop.models import OccurrenceStatus, datetime_to_iso
from replyloop.service import ReminderService

UTC = timezone.utc
CHAT_KEY = "chat" + "_id"
SENDER_KEY = "sender" + "_id"
TARGET = {"platform": "telegram", CHAT_KEY: "conversation-alpha", SENDER_KEY: "participant-alpha", "is_dm": True}


class OfflineRetryTests(unittest.TestCase):
    def test_transport_retry_is_separate_from_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            adapter = RecordingAdapter([
                DeliveryOutcome.failure("synthetic", "offline"),
                DeliveryOutcome.failure("synthetic", "still offline"),
                DeliveryOutcome.success("synthetic", "msg-ok"),
            ])
            service = ReminderService(db, adapter, clock)
            service.create_reminder(
                reminder_id="reminder-1",
                target=TARGET,
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
                intervals_minutes=(10,),
                max_deliveries=2,
            )
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            first = service.tick()
            clock.set(datetime(2026, 1, 1, 9, 0, 30, tzinfo=UTC))
            too_soon = service.tick()
            clock.set(datetime(2026, 1, 1, 9, 1, tzinfo=UTC))
            second = service.tick()
            clock.set(datetime(2026, 1, 1, 9, 5, tzinfo=UTC))
            still_too_soon = service.tick()
            clock.set(datetime(2026, 1, 1, 9, 6, tzinfo=UTC))
            third = service.tick()
            attempts = db.connection.execute("SELECT status FROM delivery_attempts ORDER BY attempted_at").fetchall()
            occurrence = db.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            events = [event.event_type for event in db.list_events()]
            db.close()
        self.assertEqual((first.attempted, first.failed), (1, 1))
        self.assertEqual(too_soon.attempted, 0)
        self.assertEqual((second.attempted, second.failed), (1, 1))
        self.assertEqual(still_too_soon.attempted, 0)
        self.assertEqual((third.attempted, third.delivered), (1, 1))
        self.assertEqual([row["status"] for row in attempts], ["failure", "failure", "success"])
        self.assertEqual(occurrence, "delivered")
        self.assertNotIn("occurrence.escalated", events)

    def test_adapter_exception_restores_occurrence_for_retry(self) -> None:
        class RaisingAdapter:
            transport = "synthetic"

            def __init__(self) -> None:
                self.requests = []

            def deliver(self, request):
                self.requests.append(request)
                raise RuntimeError("network exploded")

        with tempfile.TemporaryDirectory() as tmp:
            db = connect(Path(tmp) / "state.sqlite")
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            adapter = RaisingAdapter()
            service = ReminderService(db, adapter, clock)
            service.create_reminder(
                reminder_id="reminder-1",
                target=TARGET,
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
            )
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            result = service.tick()
            occurrence = db.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            attempts = db.connection.execute("SELECT status, error FROM delivery_attempts").fetchall()
            db.close()
        self.assertEqual((result.attempted, result.failed), (1, 1))
        self.assertEqual(occurrence, "due")
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual([row["status"] for row in attempts], ["failure"])
        self.assertEqual(attempts[0]["error"], "network exploded")

    def test_stale_delivering_claim_is_recovered_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            service = ReminderService(db, RecordingAdapter(), clock)
            service.create_reminder(
                reminder_id="reminder-1",
                target=TARGET,
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
            )
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            occurrence_id = db.connection.execute("SELECT id FROM occurrences").fetchone()["id"]
            stale_at = datetime(2026, 1, 1, 9, 1, tzinfo=UTC)
            db.connection.execute(
                "UPDATE occurrences SET status = ?, updated_at = ? WHERE id = ?",
                (OccurrenceStatus.DELIVERING.value, datetime_to_iso(stale_at), occurrence_id),
            )
            db.connection.commit()
            db.close()

            reopened = connect(path)
            adapter = RecordingAdapter()
            clock.set(datetime(2026, 1, 1, 10, 1, tzinfo=UTC))
            result = ReminderService(reopened, adapter, clock).tick()
            occurrence = reopened.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            attempts = reopened.connection.execute("SELECT status FROM delivery_attempts ORDER BY attempted_at").fetchall()
            reopened.close()
        self.assertEqual((result.attempted, result.delivered), (1, 1))
        self.assertEqual(occurrence, "delivered")
        self.assertEqual([row["status"] for row in attempts], ["success", "success"])


if __name__ == "__main__":
    unittest.main()
