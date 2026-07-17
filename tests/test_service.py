from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from replyloop.clock import FakeClock
from replyloop.db import connect
from replyloop.delivery import DeliveryOutcome, DeliveryRequest, RecordingAdapter
from replyloop.errors import ValidationError
from replyloop.models import OccurrenceStatus, ReminderStatus
from replyloop.replies import ReplyCommand, ReplyIdentity
from replyloop.service import ReminderService

UTC = timezone.utc
CHAT_KEY = "chat" + "_id"
SENDER_KEY = "sender" + "_id"
TARGET = {"platform": "telegram", CHAT_KEY: "conversation-alpha", SENDER_KEY: "participant-alpha", "is_dm": True}


class SlowRecordingAdapter:
    transport = "synthetic"

    def __init__(self) -> None:
        self.requests: list[DeliveryRequest] = []
        self._lock = threading.Lock()

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        with self._lock:
            self.requests.append(request)
            message_id = f"msg-{len(self.requests)}"
        time.sleep(0.1)
        return DeliveryOutcome.success(self.transport, message_id)


class BlockingSuccessAdapter:
    transport = "synthetic"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests: list[DeliveryRequest] = []

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        self.requests.append(request)
        self.started.set()
        self.release.wait(timeout=5)
        return DeliveryOutcome.success(self.transport, "msg-late")


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

    def test_two_workers_racing_same_due_occurrence_deliver_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            create_daily(ReminderService(db, RecordingAdapter(), clock))
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            db.close()
            adapter = SlowRecordingAdapter()
            results = []
            exceptions: list[BaseException] = []

            def run_tick() -> None:
                worker_db = None
                try:
                    worker_db = connect(path)
                    results.append(ReminderService(worker_db, adapter, clock).tick())
                except BaseException as exc:
                    exceptions.append(exc)
                finally:
                    if worker_db is not None:
                        worker_db.close()

            threads = [threading.Thread(target=run_tick) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            check = connect(path)
            attempts = check.connection.execute("SELECT status FROM delivery_attempts").fetchall()
            occurrence = check.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            check.close()
        self.assertEqual(exceptions, [])
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(sum(result.attempted for result in results), 1)
        self.assertEqual([row["status"] for row in attempts], ["success"])
        self.assertEqual(occurrence, "delivered")

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
            open_count = db.connection.execute("SELECT COUNT(*) FROM occurrences WHERE status IN ('due','delivering','delivered','snoozed')").fetchone()[0]
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

    def test_cancel_resolves_snoozed_occurrence_without_waiting_for_redelivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, _adapter, service = make_service(tmp)
            create_daily(service)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            snoozed = service.handle_reply("SNOOZE 1h", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            cancelled = service.handle_reply("CANCEL", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            occurrence = db.get_occurrence(snoozed.occurrence_id or "")
            reminder = db.get_reminder("reminder-1")
            db.close()
        self.assertTrue(snoozed.handled)
        self.assertTrue(cancelled.handled)
        assert occurrence is not None
        self.assertEqual(occurrence.status, OccurrenceStatus.CANCELLED)
        assert reminder is not None
        self.assertEqual(reminder.status, ReminderStatus.CANCELLED)

    def test_cancelled_in_flight_delivery_cannot_overwrite_cancelled_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            setup_service = ReminderService(db, RecordingAdapter(), clock)
            create_daily(setup_service, max_deliveries=2)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            setup_service.tick()
            clock.set(datetime(2026, 1, 1, 9, 10, tzinfo=UTC))
            db.close()
            adapter = BlockingSuccessAdapter()
            exceptions: list[BaseException] = []

            def run_tick() -> None:
                worker_db = None
                try:
                    worker_db = connect(path)
                    ReminderService(worker_db, adapter, clock).tick()
                except BaseException as exc:
                    exceptions.append(exc)
                finally:
                    if worker_db is not None:
                        worker_db.close()

            thread = threading.Thread(target=run_tick)
            thread.start()
            self.assertTrue(adapter.started.wait(timeout=5))
            reply_db = connect(path)
            cancelled = ReminderService(reply_db, RecordingAdapter(), clock).handle_reply(
                "CANCEL", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True)
            )
            reply_db.close()
            adapter.release.set()
            thread.join(timeout=5)
            check = connect(path)
            statuses = [row["status"] for row in check.connection.execute("SELECT status FROM occurrences").fetchall()]
            attempts = check.connection.execute("SELECT status FROM delivery_attempts ORDER BY id").fetchall()
            reminder = check.get_reminder("reminder-1")
            check.close()
        self.assertEqual(exceptions, [])
        self.assertTrue(cancelled.handled)
        self.assertEqual(statuses, ["cancelled"])
        self.assertEqual([row["status"] for row in attempts], ["success", "success"])
        assert reminder is not None
        self.assertEqual(reminder.status, ReminderStatus.CANCELLED)

    def test_racing_escalation_does_not_resurrect_done_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            service = ReminderService(db, RecordingAdapter(), clock)
            create_daily(service, max_deliveries=2)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            clock.set(datetime(2026, 1, 1, 9, 10, tzinfo=UTC))
            done = service.handle_reply("DONE", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            db.close()

            reopened = connect(path)
            result = ReminderService(reopened, RecordingAdapter(), clock).tick()
            occurrence = reopened.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            events = [event.event_type for event in reopened.list_events()]
            reopened.close()
        self.assertTrue(done.handled)
        self.assertEqual(result.attempted, 0)
        self.assertEqual(occurrence, "done")
        self.assertNotIn("occurrence.escalated", events)

    def test_two_workers_racing_escalation_queue_emit_one_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            service = ReminderService(db, RecordingAdapter(), clock)
            create_daily(service, max_deliveries=2)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            clock.set(datetime(2026, 1, 1, 9, 10, tzinfo=UTC))
            db.close()

            adapter = SlowRecordingAdapter()
            results = []
            exceptions: list[BaseException] = []

            def run_tick() -> None:
                worker_db = None
                try:
                    worker_db = connect(path)
                    results.append(ReminderService(worker_db, adapter, clock).tick())
                except BaseException as exc:
                    exceptions.append(exc)
                finally:
                    if worker_db is not None:
                        worker_db.close()

            threads = [threading.Thread(target=run_tick) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            check = connect(path)
            attempts = check.connection.execute("SELECT status FROM delivery_attempts ORDER BY attempted_at, id").fetchall()
            events = [event.event_type for event in check.list_events()]
            check.close()
        self.assertEqual(exceptions, [])
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(sum(result.attempted for result in results), 1)
        self.assertEqual([row["status"] for row in attempts], ["success", "success"])
        self.assertEqual(events.count("occurrence.escalated"), 1)

    def test_reply_resolves_latest_delivered_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, _adapter, service = make_service(tmp)
            create_daily(service)
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            service.tick()
            clock.set(datetime(2026, 1, 2, 9, 0, tzinfo=UTC))
            service.tick()
            result = service.handle_reply("DONE", ReplyIdentity("telegram", "conversation-alpha", "participant-alpha", True))
            statuses = [row["status"] for row in db.connection.execute("SELECT status FROM occurrences ORDER BY scheduled_for").fetchall()]
            db.close()
        self.assertTrue(result.handled)
        self.assertEqual(statuses, ["delivered", "done"])

    def test_tied_latest_delivered_matches_are_ambiguous(self) -> None:
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
        self.assertEqual(result.reason, "ambiguous")
        self.assertEqual(statuses, ["delivered", "delivered"])

    def test_create_reminder_rejects_invalid_lifecycle_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, _clock, _adapter, service = make_service(tmp)
            try:
                invalid_cases = [
                    {"default_snooze_minutes": True},
                    {"default_snooze_minutes": 600000},
                    {"intervals_minutes": (False,)},
                    {"max_deliveries": True},
                    {"repeat_last": "yes"},
                ]
                for index, overrides in enumerate(invalid_cases):
                    with self.subTest(overrides=overrides):
                        kwargs = {
                            "reminder_id": f"reminder-invalid-{index}",
                            "target": TARGET,
                            "schedule": {"kind": "daily", "times": ["09:00"]},
                            "timezone": "UTC",
                        }
                        kwargs.update(overrides)
                        with self.assertRaises(ValidationError):
                            service.create_reminder(**kwargs)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
