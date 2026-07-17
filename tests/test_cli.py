from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from replyloop.clock import FakeClock
from replyloop.db import connect
from replyloop.delivery import RecordingAdapter
from replyloop.models import Occurrence, OccurrenceStatus, Reminder, datetime_to_iso
from replyloop.service import ReminderService

ROOT = Path(__file__).resolve().parents[1]
CHAT_KEY = "chat" + "_id"
SENDER_KEY = "sender" + "_id"
UTC = timezone.utc


def run_cli(tmp: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["REPLYLOOP_DB"] = str(Path(tmp) / "state.db")
    return subprocess.run([sys.executable, "-m", "replyloop", *args], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def seed_due(tmp: str, reminder_id: str, chat: str, sender: str | None = None) -> None:
    path = Path(tmp) / "state.db"
    db = connect(path)
    clock = FakeClock(datetime(2020, 1, 1, 0, 0, tzinfo=UTC))
    target = {"platform": "telegram", CHAT_KEY: chat, "is_dm": True}
    if sender is not None:
        target[SENDER_KEY] = sender
    ReminderService(db, RecordingAdapter(), clock).create_reminder(
        reminder_id=reminder_id,
        target=target,
        schedule={"kind": "once", "at": "2020-01-01T00:01:00Z"},
        timezone="UTC",
    )
    db.close()


class CLITests(unittest.TestCase):
    def test_create_list_show_json_roundtrip_without_target_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            created = run_cli(tmp, "--json", "create", "--id", "r1", "--daily", "--time", "09:00", "--timezone", "UTC", "--platform", "telegram", "--chat", "c1", "--sender", "s1", "--snooze", "15", "--escalation", "10", "--max-deliveries", "2")
            listed = run_cli(tmp, "--json", "list")
            shown = run_cli(tmp, "--json", "show", "r1")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        payload = json.loads(created.stdout)
        self.assertEqual(payload["reminder"]["id"], "r1")
        self.assertEqual(payload["reminder"]["escalation_minutes"], [10])
        self.assertNotIn("c1", created.stdout + listed.stdout + shown.stdout)
        self.assertEqual(json.loads(listed.stdout)["reminders"][0]["id"], "r1")
        self.assertEqual(json.loads(shown.stdout)["occurrences"], [])

    def test_create_accepts_schedule_json_and_actionable_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok = run_cli(tmp, "--json", "create", "--id", "r2", "--schedule-json", '{"kind":"once","at":"2026-01-01T09:00:00Z"}', "--timezone", "UTC", "--target", json.dumps({"platform": "telegram", CHAT_KEY: "c2"}))
            bad = run_cli(tmp, "--json", "create", "--id", "bad", "--daily", "--timezone", "UTC", "--platform", "telegram", "--chat", "c2")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("--daily requires", json.loads(bad.stderr)["error"]["message"])

    def test_pause_resume_cancel_and_missing_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_cli(tmp, "create", "--id", "r3", "--once-at", "2026-01-01T09:00:00Z", "--timezone", "UTC", "--platform", "telegram", "--chat", "c3").returncode, 0)
            paused = run_cli(tmp, "--json", "pause", "r3")
            resumed = run_cli(tmp, "--json", "resume", "r3")
            cancelled = run_cli(tmp, "--json", "cancel", "r3")
            missing = run_cli(tmp, "--json", "show", "nope")
        self.assertEqual(json.loads(paused.stdout)["reminder"]["status"], "paused")
        self.assertEqual(json.loads(resumed.stdout)["reminder"]["status"], "active")
        self.assertEqual(json.loads(cancelled.stdout)["reminder"]["status"], "cancelled")
        self.assertEqual(missing.returncode, 1)

    def test_status_transitions_reject_cancelled_resume_and_double_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_cli(tmp, "create", "--id", "r-status", "--once-at", "2020-01-01T00:01:00Z", "--timezone", "UTC", "--platform", "telegram", "--chat", "c-status").returncode, 0)
            paused = run_cli(tmp, "--json", "pause", "r-status")
            double_pause = run_cli(tmp, "--json", "pause", "r-status")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertEqual(double_pause.returncode, 1)
        self.assertIn("cannot pause", json.loads(double_pause.stderr)["error"]["message"])

        with tempfile.TemporaryDirectory() as tmp:
            seed_due(tmp, "r-cancel", "c-cancel")
            delivered = run_cli(tmp, "--json", "tick")
            cancelled = run_cli(tmp, "--json", "cancel", "r-cancel")
            resume_cancelled = run_cli(tmp, "--json", "resume", "r-cancel")
            shown = run_cli(tmp, "--json", "show", "r-cancel")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        self.assertEqual(resume_cancelled.returncode, 1)
        self.assertIn("cannot resume", json.loads(resume_cancelled.stderr)["error"]["message"])
        occurrences = json.loads(shown.stdout)["occurrences"]
        self.assertEqual({item["status"] for item in occurrences}, {"cancelled"})

    def test_tick_stdout_adapter_success_failure_and_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_due(tmp, "r4", "c4", "s4")
            success = run_cli(tmp, "--json", "tick")
            reply = run_cli(tmp, "--json", "reply", "--platform", "telegram", "--chat", "c4", "--sender", "s4", "--chat-type", "dm", "DONE")
        self.assertEqual(success.returncode, 0, success.stderr)
        success_payload = json.loads(success.stdout)
        self.assertEqual(success_payload["tick"]["delivered"], 1)
        self.assertEqual(len(success_payload["deliveries"]), 1)
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertTrue(json.loads(reply.stdout)["reply"]["handled"])

        with tempfile.TemporaryDirectory() as tmp:
            seed_due(tmp, "r5", "c5")
            failed = run_cli(tmp, "--json", "tick", "--fail")
            retry = run_cli(tmp, "--json", "doctor")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout)["tick"]["failed"], 1)
        self.assertEqual(json.loads(retry.stdout)["doctor"]["counts"]["retry_queue"], 1)

    def test_doctor_retry_queue_excludes_resolved_successful_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            seed_due(tmp, "r-retry", "c-retry")
            failed = run_cli(tmp, "--json", "tick", "--fail")
            path = Path(tmp) / "state.db"
            with connect(path) as db:
                old = datetime_to_iso(datetime.now(UTC) - timedelta(minutes=5))
                db.connection.execute("UPDATE delivery_attempts SET attempted_at = ?, created_at = ?", (old, old))
                db.connection.commit()
            recovered = run_cli(tmp, "--json", "tick")
            doctor = run_cli(tmp, "--json", "doctor")
        self.assertEqual(failed.returncode, 1, failed.stderr)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["tick"]["delivered"], 1)
        self.assertEqual(json.loads(doctor.stdout)["doctor"]["counts"]["retry_queue"], 0)

    def test_default_path_uses_xdg_data_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env.pop("REPLYLOOP_DB", None)
            env["XDG_DATA_HOME"] = str(Path(tmp) / "xdg")
            env["PYTHONPATH"] = str(ROOT)
            result = subprocess.run([sys.executable, "-m", "replyloop", "--json", "doctor"], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            expected = Path(tmp) / "xdg" / "replyloop" / "replyloop.db"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(expected.exists())

    def test_global_options_are_accepted_after_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli(tmp, "doctor", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["doctor"]["ok"])

    def test_doctor_json_and_human_output_shape_match_for_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            db = connect(path)
            due_at = datetime(2020, 1, 1, 0, 1, tzinfo=UTC)
            future_due_at = datetime(2099, 1, 1, 0, 1, tzinfo=UTC)
            db.add_reminder(Reminder("r-doctor-due", json.dumps({"platform": "telegram", CHAT_KEY: "conversation-due"}), {"kind": "once", "at": "2020-01-01T00:01:00Z"}, "UTC"))
            db.add_reminder(Reminder("r-doctor-snoozed", json.dumps({"platform": "telegram", CHAT_KEY: "conversation-snoozed"}), {"kind": "once", "at": "2099-01-01T00:01:00Z"}, "UTC"))
            db.add_occurrence(Occurrence("o-doctor-due", "r-doctor-due", due_at, OccurrenceStatus.DUE, due_at=due_at))
            db.add_occurrence(Occurrence("o-doctor-snoozed", "r-doctor-snoozed", due_at, OccurrenceStatus.SNOOZED, due_at=future_due_at))
            db.close()
            json_result = run_cli(tmp, "--json", "doctor")
            human_result = run_cli(tmp, "doctor")
        self.assertEqual(json_result.returncode, 0, json_result.stderr)
        self.assertEqual(human_result.returncode, 0, human_result.stderr)
        self.assertEqual(json.loads(json_result.stdout)["doctor"]["counts"], {"due": 1, "pending_reminders": 2, "retry_queue": 0})
        self.assertEqual(json.loads(json_result.stdout)["doctor"]["counts"], json.loads(human_result.stdout)["doctor"]["counts"])
        self.assertEqual(json.loads(json_result.stdout)["doctor"].keys(), json.loads(human_result.stdout)["doctor"].keys())


if __name__ == "__main__":
    unittest.main()
