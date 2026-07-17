from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from replyloop.clock import FakeClock
from replyloop.db import connect
from replyloop.delivery import RecordingAdapter
from replyloop.hermes_plugin.hooks import pre_gateway_dispatch
from replyloop.models import OccurrenceStatus
from replyloop.service import ReminderService


@dataclass(frozen=True)
class PlatformValue:
    value: str


class FakeAdapter:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, chat_id, content, **kwargs):
        self.sent.append({"chat" + "_id": chat_id, "content": content, **kwargs})
        return SimpleNamespace(success=True, message_id="ack-1")


class FakeGateway:
    def __init__(self, *, default_adapter=None, profile_adapters=None) -> None:
        self.adapters = {"photon": default_adapter} if default_adapter is not None else {}
        self.profile_adapters = profile_adapters or {}
        self.replyloop_redacted_skip_logging = True

    def _adapter_for_source(self, source):
        profile = getattr(source, "profile", None)
        if profile is not None:
            return self.profile_adapters.get(profile)
        return self.adapters.get("photon")


class HermesHookTests(unittest.TestCase):
    def test_photon_done_is_consumed_and_ack_scheduled_without_llm_path(self) -> None:
        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                db_path = self._seed_delivered(Path(tmp) / "state.sqlite")
                old = os.environ.get("REPLYLOOP_DB")
                os.environ["REPLYLOOP_DB"] = str(db_path)
                try:
                    adapter = FakeAdapter()
                    event = self._event("DONE", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a")
                    result = pre_gateway_dispatch(event=event, gateway=SimpleNamespace(adapters={PlatformValue("photon"): adapter}, replyloop_redacted_skip_logging=True))
                    await asyncio.sleep(0)
                    db = connect(db_path)
                    status = db.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
                    db.close()
                finally:
                    if old is None:
                        os.environ.pop("REPLYLOOP_DB", None)
                    else:
                        os.environ["REPLYLOOP_DB"] = old
            return result, adapter.sent, status

        result, sent, status = asyncio.run(run_case())
        self.assertEqual(result, {"action": "skip", "reason": "replyloop-command-handled"})
        self.assertEqual(status, OccurrenceStatus.DONE.value)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], "c-a")

    def test_photon_ack_uses_secondary_profile_adapter(self) -> None:
        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                db_path = self._seed_delivered(Path(tmp) / "state.sqlite")
                old = os.environ.get("REPLYLOOP_DB")
                os.environ["REPLYLOOP_DB"] = str(db_path)
                try:
                    default_adapter = FakeAdapter()
                    secondary_adapter = FakeAdapter()
                    event = self._event("DONE", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a", profile="secondary")
                    result = pre_gateway_dispatch(
                        event=event,
                        gateway=FakeGateway(default_adapter=default_adapter, profile_adapters={"secondary": secondary_adapter}),
                    )
                    await asyncio.sleep(0)
                finally:
                    if old is None:
                        os.environ.pop("REPLYLOOP_DB", None)
                    else:
                        os.environ["REPLYLOOP_DB"] = old
            return result, default_adapter.sent, secondary_adapter.sent

        result, default_sent, secondary_sent = asyncio.run(run_case())
        self.assertEqual(result, {"action": "skip", "reason": "replyloop-command-handled"})
        self.assertEqual(default_sent, [])
        self.assertEqual(len(secondary_sent), 1)
        self.assertEqual(secondary_sent[0]["chat_id"], "c-a")

    def test_snooze_and_cancel_exact_photon_commands_are_consumed(self) -> None:
        for command, expected in (("SNOOZE", OccurrenceStatus.SNOOZED.value), ("CANCEL", OccurrenceStatus.CANCELLED.value)):
            with self.subTest(command=command):
                async def run_case():
                    with tempfile.TemporaryDirectory() as tmp:
                        db_path = self._seed_delivered(Path(tmp) / "state.sqlite")
                        old = os.environ.get("REPLYLOOP_DB")
                        os.environ["REPLYLOOP_DB"] = str(db_path)
                        try:
                            adapter = FakeAdapter()
                            result = pre_gateway_dispatch(
                                event=self._event(command, platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a"),
                                gateway=SimpleNamespace(adapters={PlatformValue("photon"): adapter}, replyloop_redacted_skip_logging=True),
                            )
                            await asyncio.sleep(0)
                            db = connect(db_path)
                            status = db.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
                            db.close()
                        finally:
                            if old is None:
                                os.environ.pop("REPLYLOOP_DB", None)
                            else:
                                os.environ["REPLYLOOP_DB"] = old
                    return result, status
                result, status = asyncio.run(run_case())
                self.assertEqual(result["action"], "skip")
                self.assertEqual(status, expected)

    def test_other_identities_groups_and_unrelated_text_are_not_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._seed_delivered(Path(tmp) / "state.sqlite")
            old = os.environ.get("REPLYLOOP_DB")
            os.environ["REPLYLOOP_DB"] = str(db_path)
            try:
                cases = [
                    self._event("DONE please", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a"),
                    self._event("DONE", platform="telegram", chat_type="dm", chat_id="c-a", user_id="s-a"),
                    self._event("DONE", platform="photon", chat_type="group", chat_id="c-a", user_id="s-a"),
                    self._event("DONE", platform="photon", chat_type="dm", chat_id="c-b", user_id="s-a"),
                    self._event("DONE", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-b"),
                ]
                results = [pre_gateway_dispatch(event=case, gateway=SimpleNamespace(adapters={})) for case in cases]
            finally:
                if old is None:
                    os.environ.pop("REPLYLOOP_DB", None)
                else:
                    os.environ["REPLYLOOP_DB"] = old
        self.assertEqual(results, [None, None, None, None, None])

    def test_ambiguous_equally_latest_occurrences_are_not_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._seed_ambiguous_delivered(Path(tmp) / "state.sqlite")
            old = os.environ.get("REPLYLOOP_DB")
            os.environ["REPLYLOOP_DB"] = str(db_path)
            try:
                adapter = FakeAdapter()
                result = pre_gateway_dispatch(
                    event=self._event("DONE", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a"),
                    gateway=SimpleNamespace(adapters={"photon": adapter}, replyloop_redacted_skip_logging=True),
                )
                db = connect(db_path)
                statuses = [row["status"] for row in db.connection.execute("SELECT status FROM occurrences ORDER BY id").fetchall()]
                db.close()
            finally:
                if old is None:
                    os.environ.pop("REPLYLOOP_DB", None)
                else:
                    os.environ["REPLYLOOP_DB"] = old
        self.assertIsNone(result)
        self.assertEqual(adapter.sent, [])
        self.assertEqual(statuses, [OccurrenceStatus.DELIVERED.value, OccurrenceStatus.DELIVERED.value])

    def test_successful_db_mutation_without_ack_allows_normal_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._seed_delivered(Path(tmp) / "state.sqlite")
            old = os.environ.get("REPLYLOOP_DB")
            os.environ["REPLYLOOP_DB"] = str(db_path)
            try:
                result = pre_gateway_dispatch(
                    event=self._event("DONE", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a"),
                    gateway=SimpleNamespace(adapters={}),
                )
            finally:
                if old is None:
                    os.environ.pop("REPLYLOOP_DB", None)
                else:
                    os.environ["REPLYLOOP_DB"] = old
        self.assertEqual(result["action"], "allow")
        self.assertIn("replyloop-ack-unavailable", result["reason"])

    def test_secondary_profile_missing_adapter_does_not_use_default_adapter(self) -> None:
        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                db_path = self._seed_delivered(Path(tmp) / "state.sqlite")
                old = os.environ.get("REPLYLOOP_DB")
                os.environ["REPLYLOOP_DB"] = str(db_path)
                try:
                    default_adapter = FakeAdapter()
                    event = self._event("DONE", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a", profile="secondary")
                    result = pre_gateway_dispatch(
                        event=event,
                        gateway=FakeGateway(default_adapter=default_adapter, profile_adapters={}),
                    )
                    await asyncio.sleep(0)
                finally:
                    if old is None:
                        os.environ.pop("REPLYLOOP_DB", None)
                    else:
                        os.environ["REPLYLOOP_DB"] = old
            return result, default_adapter.sent

        result, default_sent = asyncio.run(run_case())
        self.assertEqual(result["action"], "allow")
        self.assertIn("replyloop-ack-unavailable", result["reason"])
        self.assertEqual(default_sent, [])

    def test_skip_requires_redacted_gateway_logging_prerequisite(self) -> None:
        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                db_path = self._seed_delivered(Path(tmp) / "state.sqlite")
                old = os.environ.get("REPLYLOOP_DB")
                os.environ["REPLYLOOP_DB"] = str(db_path)
                try:
                    adapter = FakeAdapter()
                    event = self._event("DONE", platform="photon", chat_type="dm", chat_id="c-a", user_id="s-a")
                    result = pre_gateway_dispatch(event=event, gateway=SimpleNamespace(adapters={"photon": adapter}))
                    await asyncio.sleep(0)
                finally:
                    if old is None:
                        os.environ.pop("REPLYLOOP_DB", None)
                    else:
                        os.environ["REPLYLOOP_DB"] = old
            return result, adapter.sent

        result, sent = asyncio.run(run_case())
        self.assertEqual(result, {"action": "allow", "reason": "replyloop-command-handled-redaction-prerequisite-missing"})
        self.assertEqual(len(sent), 1)

    def _seed_delivered(self, db_path: Path) -> Path:
        clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc))
        db = connect(db_path)
        service = ReminderService(db, RecordingAdapter(), clock)
        service.create_reminder(
            reminder_id="r1",
            target={"platform": "photon", "chat_id": "c-a", "sender_id": "s-a", "is_dm": True},
            schedule={"kind": "daily", "times": ["09:00"]},
            timezone="UTC",
        )
        clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
        service.tick()
        db.close()
        return db_path

    def _seed_ambiguous_delivered(self, db_path: Path) -> Path:
        clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc))
        db = connect(db_path)
        service = ReminderService(db, RecordingAdapter(), clock)
        for reminder_id in ("r1", "r2"):
            service.create_reminder(
                reminder_id=reminder_id,
                target={"platform": "photon", "chat_id": "c-a", "sender_id": "s-a", "is_dm": True},
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
            )
        clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
        service.tick()
        same_attempted_at = "2026-01-01T09:00:00+00:00"
        db.connection.execute("UPDATE delivery_attempts SET attempted_at = ?", (same_attempted_at,))
        db.connection.commit()
        db.close()
        return db_path

    def _event(self, text: str, *, platform: str, chat_type: str, chat_id: str, user_id: str, profile: str | None = None):
        source = SimpleNamespace(platform=PlatformValue(platform), chat_type=chat_type)
        setattr(source, "chat" + "_id", chat_id)
        setattr(source, "user" + "_id", user_id)
        if profile is not None:
            setattr(source, "profile", profile)
        return SimpleNamespace(text=text, source=source)


if __name__ == "__main__":
    unittest.main()
