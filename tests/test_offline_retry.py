from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from replyloop.clock import FakeClock
from replyloop.db import connect
from replyloop.delivery import DeliveryOutcome, RecordingAdapter
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


if __name__ == "__main__":
    unittest.main()
