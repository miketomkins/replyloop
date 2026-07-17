from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from replyloop.clock import FakeClock
from replyloop.db import connect
from replyloop.delivery import DeliveryOutcome, DeliveryRequest, RecordingAdapter
from replyloop.models import OccurrenceStatus, datetime_to_iso
from replyloop.service import ReminderService

UTC = timezone.utc
CHAT_KEY = "chat" + "_id"
SENDER_KEY = "sender" + "_id"
TARGET = {"platform": "telegram", CHAT_KEY: "conversation-alpha", SENDER_KEY: "participant-alpha", "is_dm": True}


class BlockingSuccessAdapter:
    transport = "synthetic"

    def __init__(self, provider_message_id: str) -> None:
        self.provider_message_id = provider_message_id
        self.started = threading.Event()
        self.release = threading.Event()
        self.requests: list[DeliveryRequest] = []

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        self.requests.append(request)
        self.started.set()
        self.release.wait(timeout=5)
        return DeliveryOutcome.success(self.transport, self.provider_message_id)


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

    def test_expired_claim_id_prevents_late_worker_from_consuming_replacement_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db = connect(path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            setup = ReminderService(db, RecordingAdapter(), clock)
            setup.create_reminder(
                reminder_id="reminder-1",
                target=TARGET,
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
            )
            db.close()

            original_adapter = BlockingSuccessAdapter("msg-original")
            exceptions: list[BaseException] = []

            def run_original() -> None:
                worker_db = None
                try:
                    worker_db = connect(path)
                    clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
                    ReminderService(worker_db, original_adapter, clock).tick()
                except BaseException as exc:
                    exceptions.append(exc)
                finally:
                    if worker_db is not None:
                        worker_db.close()

            thread = threading.Thread(target=run_original)
            thread.start()
            self.assertTrue(original_adapter.started.wait(timeout=5))

            recovery_db = connect(path)
            recovery_adapter = RecordingAdapter([DeliveryOutcome.success("synthetic", "msg-replacement")])
            clock.set(datetime(2026, 1, 1, 10, 0, 1, tzinfo=UTC))
            recovery = ReminderService(recovery_db, recovery_adapter, clock).tick()
            recovery_db.close()

            original_adapter.release.set()
            thread.join(timeout=5)

            check = connect(path)
            occurrence = check.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            attempts = check.connection.execute("SELECT status FROM delivery_attempts ORDER BY attempted_at, id").fetchall()
            success_events = [event.payload for event in check.list_events() if event.event_type == "delivery.succeeded"]
            check.close()
        self.assertEqual(exceptions, [])
        self.assertEqual((recovery.attempted, recovery.delivered), (1, 1))
        self.assertEqual(occurrence, "delivered")
        self.assertEqual(len(original_adapter.requests), 1)
        self.assertEqual(len(recovery_adapter.requests), 1)
        self.assertEqual([row["status"] for row in attempts], ["success", "success"])
        self.assertEqual([event["applied"] for event in success_events], [True, False])


if __name__ == "__main__":
    unittest.main()
