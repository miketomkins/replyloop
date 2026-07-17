from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from replyloop.clock import FakeClock
from replyloop.db import connect
from replyloop.delivery import DeliveryOutcome, RecordingAdapter
from replyloop.models import OccurrenceStatus, ReminderStatus
from replyloop.replies import ReplyCommand, ReplyIdentity
from replyloop.service import ReminderService

UTC = timezone.utc
CHAT_KEY = "chat" + "_id"
SENDER_KEY = "sender" + "_id"
TARGET = {"platform": "telegram", CHAT_KEY: "conversation-alpha", SENDER_KEY: "participant-alpha", "is_dm": True}


def make_service(tmp: str, *, outcomes: list[DeliveryOutcome] | None = None, now: datetime | None = None):
    db = connect(Path(tmp) / "state.sqlite")
    clock = FakeClock(now or datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
    adapter = RecordingAdapter(outcomes)
    return db, clock, adapter, ReminderService(db, adapter, clock)


def create_daily(service: ReminderService, *, max_deliveries: int = 1, repeat_last: bool = False) -> None:
    service.create_reminder(
        reminder_id="reminder-1",
        target=TARGET,
        schedule={"kind": "daily", "times": ["09:00"]},
        timezone="UTC",
        default_snooze_minutes=30,
        intervals_minutes=(10,),
        max_deliveries=max_deliveries,
        repeat_last=repeat_last,
    )


class ServiceLifecycleTests(unittest.TestCase):
    def test_tick_creates_and_delivers_occurrence_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, adapter, service = make_service(tmp)
            create_daily(service)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            first = service.tick()
            second = service.tick()
            occurrences = db.connection.execute("SELECT * FROM occurrences").fetchall()
            events = [event.event_type for event in db.list_events()]
            db.close()
        self.assertEqual(first.created, 1)
        self.assertEqual(second.created, 0)
        self.assertEqual(len(occurrences), 1)
        self.assertEqual(len(adapter.requests), 1)
        self.assertIn("delivery.succeeded", events)

    def test_restart_does_not_duplicate_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, _adapter, service = make_service(tmp)
            create_daily(service)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            db.close()
            reopened = connect(Path(tmp) / "state.sqlite")
            adapter = RecordingAdapter()
            restarted = ReminderService(reopened, adapter, clock)
            restarted.tick()
            count = reopened.connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
            reopened.close()
        self.assertEqual(count, 1)
        self.assertEqual(adapter.requests, [])

    def test_two_workers_racing_same_tick_create_one_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            first = ReminderService(db, RecordingAdapter(), clock)
            create_daily(first)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            second_db = connect(path)
            second = ReminderService(second_db, RecordingAdapter(), clock)
            first.tick()
            second.tick()
            count = db.connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
            db.close()
            second_db.close()
        self.assertEqual(count, 1)

    def test_successful_delivery_starts_escalation_clock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, adapter, service = make_service(tmp)
            create_daily(service, max_deliveries=2)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            clock.set(datetime(2026, 1, 1, 9, 9, tzinfo=UTC))
            service.tick()
            clock.set(datetime(2026, 1, 1, 9, 10, tzinfo=UTC))
            service.tick()
            attempts = db.connection.execute("SELECT status FROM delivery_attempts").fetchall()
            events = [event.event_type for event in db.list_events()]
            db.close()
        self.assertEqual(len(adapter.requests), 2)
        self.assertEqual([row["status"] for row in attempts], ["success", "success"])
        self.assertIn("occurrence.escalated", events)

    def test_done_snooze_and_cancel_mutate_only_matching_delivered_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, _adapter, service = make_service(tmp)
            create_daily(service, max_deliveries=3, repeat_last=True)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            wrong = service.handle_reply("DONE", ReplyIdentity("telegram", "conversation-alpha", "participant-beta", True))
            group = service.handle_reply("DONE", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", False))
            snoozed = service.handle_reply("SNOOZE 1h", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            occurrence_id = snoozed.occurrence_id
            assert occurrence_id is not None
            snoozed_occ = db.get_occurrence(occurrence_id)
            clock.set(datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
            service.tick()
            done = service.handle_reply("done", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            clock.set(datetime(2026, 1, 2, 9, 0, tzinfo=UTC))
            service.tick()
            cancelled = service.handle_reply("cancel", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            reminder = db.get_reminder("reminder-1")
            open_count = db.connection.execute("SELECT COUNT(*) FROM occurrences WHERE status IN ('due','delivered')").fetchone()[0]
            db.close()
        self.assertFalse(wrong.handled)
        self.assertFalse(group.handled)
        self.assertTrue(snoozed.handled)
        assert snoozed_occ is not None
        self.assertEqual(snoozed_occ.status, OccurrenceStatus.SNOOZED)
        self.assertTrue(done.handled)
        self.assertTrue(cancelled.handled)
        self.assertEqual(cancelled.command, ReplyCommand.CANCEL)
        assert reminder is not None
        self.assertEqual(reminder.status, ReminderStatus.CANCELLED)
        self.assertEqual(open_count, 0)

    def test_ambiguous_latest_delivered_matches_do_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, _adapter, service = make_service(tmp)
            create_daily(service)
            service.create_reminder(
                reminder_id="reminder-2",
                target=TARGET,
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
            )
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            result = service.handle_reply("DONE", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            statuses = [row["status"] for row in db.connection.execute("SELECT status FROM occurrences ORDER BY id").fetchall()]
            db.close()
        self.assertFalse(result.handled)
        self.assertEqual(statuses, ["delivered", "delivered"])


if __name__ == "__main__":
    unittest.main()
