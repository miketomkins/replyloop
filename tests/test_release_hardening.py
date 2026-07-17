from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from replyloop.clock import FakeClock
from replyloop.cli import doctor
from replyloop.db import connect
from replyloop.delivery import DeliveryOutcome, RecordingAdapter
from replyloop.hermes_plugin.hooks import pre_gateway_dispatch
from replyloop.models import OccurrenceStatus, ReminderStatus
from replyloop.replies import ReplyIdentity
from replyloop.schedules import due_times_between
from replyloop.service import ReminderService

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
CHAT_KEY = "chat" + "_id"
SENDER_KEY = "sender" + "_id"
TARGET = {"platform": "photon", CHAT_KEY: "conversation-alpha", SENDER_KEY: "participant-alpha", "is_dm": True}


class ReleaseHardeningTests(unittest.TestCase):
    def test_duplicate_tick_and_racing_tick_emit_one_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
            db = connect(path)
            service = ReminderService(db, RecordingAdapter(), clock)
            service.create_reminder(reminder_id="r1", target=TARGET, schedule={"kind": "daily", "times": ["09:00"]}, timezone="UTC")
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
            first = service.tick()
            second = service.tick()
            other = connect(path)
            third = ReminderService(other, RecordingAdapter(), clock).tick()
            count = db.connection.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0]
            attempts = db.connection.execute("SELECT COUNT(*) FROM delivery_attempts WHERE status = 'success'").fetchone()[0]
            db.close()
            other.close()
        self.assertEqual((first.created, first.attempted, first.delivered), (1, 1, 1))
        self.assertEqual((second.created, second.attempted, second.delivered), (0, 0, 0))
        self.assertEqual((third.created, third.attempted, third.delivered), (0, 0, 0))
        self.assertEqual((count, attempts), (1, 1))

    def test_dst_gap_and_fold_are_deterministic(self) -> None:
        gap = due_times_between(
            {"kind": "daily", "times": ["02:30"]},
            "America/New_York",
            datetime(2026, 3, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 9, 0, 0, tzinfo=UTC),
        )
        fold = due_times_between(
            {"kind": "daily", "times": ["01:30"]},
            "America/New_York",
            datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(gap, [])
        self.assertEqual(fold, [datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)])

    def test_wrong_sender_and_group_replies_do_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db, clock, service = self._delivered_service(Path(tmp) / "state.sqlite")
            wrong = service.handle_reply("DONE", ReplyIdentity("photon", "conversation-alpha", "participant-beta", True))
            group = service.handle_reply("DONE", ReplyIdentity("photon", "conversation-alpha", "participant-alpha", False))
            status = db.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            db.close()
        self.assertFalse(wrong.handled)
        self.assertFalse(group.handled)
        self.assertEqual(status, OccurrenceStatus.DELIVERED.value)

    def test_database_failure_is_reported_by_doctor_without_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.sqlite"
            path.write_text("not sqlite", encoding="utf-8")
            result = doctor(path)
        self.assertFalse(result["doctor"]["ok"])
        self.assertTrue(any(item["name"] == "database" and not item["ok"] for item in result["doctor"]["checks"]))

    def test_photon_ack_outage_preserves_db_transition_and_recovery_can_handle_next_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            db, clock, _service = self._delivered_service(db_path)
            db.close()
            old = os.environ.get("REPLYLOOP_DB")
            os.environ["REPLYLOOP_DB"] = str(db_path)
            try:
                outage = pre_gateway_dispatch(event=self._event("DONE"), gateway=SimpleNamespace(adapters={}))
            finally:
                if old is None:
                    os.environ.pop("REPLYLOOP_DB", None)
                else:
                    os.environ["REPLYLOOP_DB"] = old
            self.assertIsNotNone(outage)
            reopened = connect(db_path)
            status = reopened.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            reminder = reopened.get_reminder("r1")
            assert reminder is not None
            clock.set(datetime(2026, 1, 2, 9, 0, tzinfo=UTC))
            adapter = RecordingAdapter()
            recovered = ReminderService(reopened, adapter, clock).tick()
            reopened.close()
        assert outage is not None
        self.assertEqual(outage["action"], "allow")
        self.assertEqual(status, OccurrenceStatus.DONE.value)
        self.assertEqual((recovered.created, recovered.delivered), (1, 1))

    def test_snooze_and_cancel_race_closes_open_occurrences(self) -> None:
        self._assert_snooze_cancel_race(snooze_delay=0.0, cancel_delay=0.05, expected_snooze_handled=True)
        self._assert_snooze_cancel_race(snooze_delay=0.05, cancel_delay=0.0, expected_snooze_handled=False)

    def _assert_snooze_cancel_race(self, *, snooze_delay: float, cancel_delay: float, expected_snooze_handled: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite"
            db, clock, _service = self._delivered_service(path)
            db.close()
            barrier = threading.Barrier(2)
            results = {}

            def reply(name: str, text: str, delay: float) -> None:
                local_db = connect(path)
                try:
                    service = ReminderService(local_db, RecordingAdapter(), clock)
                    barrier.wait(timeout=5)
                    if delay:
                        time.sleep(delay)
                    results[name] = service.handle_reply(text, ReplyIdentity("photon", "conversation-alpha", "participant-alpha", True))
                finally:
                    local_db.close()

            threads = [
                threading.Thread(target=reply, args=("snooze", "SNOOZE 30m", snooze_delay)),
                threading.Thread(target=reply, args=("cancel", "CANCEL", cancel_delay)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse([thread for thread in threads if thread.is_alive()])
            db = connect(path)
            reminder = db.get_reminder("r1")
            open_count = db.connection.execute("SELECT COUNT(*) FROM occurrences WHERE status IN ('due','delivering','delivered','snoozed')").fetchone()[0]
            occurrence_statuses = [row["status"] for row in db.connection.execute("SELECT status FROM occurrences ORDER BY id").fetchall()]
            db.close()
        self.assertEqual(set(results), {"snooze", "cancel"})
        self.assertTrue(results["cancel"].handled)
        self.assertEqual(results["snooze"].handled, expected_snooze_handled)
        if not results["snooze"].handled:
            self.assertEqual(results["snooze"].reason, "not-found")
        assert reminder is not None
        self.assertEqual(reminder.status, ReminderStatus.CANCELLED)
        self.assertEqual(open_count, 0)
        self.assertTrue(occurrence_statuses)
        self.assertTrue(all(status == OccurrenceStatus.CANCELLED.value for status in occurrence_statuses))

    def test_corrupt_database_cli_doctor_smoke_fails_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.sqlite"
            path.write_bytes(b"not sqlite")
            result = subprocess.run(
                [sys.executable, "-m", "replyloop", "--db", str(path), "--json", "doctor"],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('"ok": false', result.stdout)
        self.assertNotIn(str(path), result.stderr)

    def test_wheel_metadata_declares_empty_runtime_dependencies_and_entry_points(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dependencies = []', pyproject)
        self.assertIn('replyloop = "replyloop.cli:main"', pyproject)
        self.assertIn('[project.entry-points."hermes_agent.plugins"]', pyproject)
        self.assertIn('replyloop = "replyloop.hermes_plugin"', pyproject)
        self.assertIn('build-backend = "setuptools.build_meta"', pyproject)

    def test_ci_workflow_shape_runs_release_contract_steps(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for version in ("'3.11'", "'3.12'", "'3.13'"):
            self.assertIn(version, workflow)
        for required in (
            "scripts/public_repo_audit.py .",
            "unittest discover",
            "compileall -q replyloop",
            "pip wheel --no-deps --no-build-isolation",
            "venv",
            "replyloop\" --json doctor",
        ):
            self.assertIn(required, workflow)

    def test_no_skipped_tests_or_committed_build_outputs(self) -> None:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, text=True, stdout=subprocess.PIPE, check=True).stdout.splitlines()
        forbidden_suffixes = (".pyc", ".db", ".sqlite", ".sqlite3", ".log")
        self.assertFalse([path for path in tracked if path.startswith(("dist/", "build/")) or path.endswith(forbidden_suffixes)])
        test_text = "\n".join((ROOT / path).read_text(encoding="utf-8") for path in tracked if path.startswith("tests/") and path.endswith(".py"))
        self.assertNotIn("@unittest." + "skip", test_text)
        self.assertNotIn("pytest.mark." + "skip", test_text)

    def _delivered_service(self, path: Path):
        clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=UTC))
        db = connect(path)
        service = ReminderService(db, RecordingAdapter(), clock)
        service.create_reminder(reminder_id="r1", target=TARGET, schedule={"kind": "daily", "times": ["09:00"]}, timezone="UTC")
        clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=UTC))
        service.tick()
        return db, clock, service

    def _event(self, text: str):
        source = SimpleNamespace(platform="photon", chat_type="dm")
        setattr(source, CHAT_KEY, "conversation-alpha")
        setattr(source, "user" + "_id", "participant-alpha")
        return SimpleNamespace(text=text, source=source)


if __name__ == "__main__":
    unittest.main()
