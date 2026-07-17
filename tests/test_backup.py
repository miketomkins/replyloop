from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from replyloop.cli import backup_database, doctor
from replyloop.db import connect
from replyloop.models import Occurrence, OccurrenceStatus, Reminder

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


class BackupTests(unittest.TestCase):
    def test_backup_uses_reopenable_integrity_checked_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            destination = Path(tmp) / "copy.db"
            db = connect(source)
            db.add_reminder(Reminder("r1", '{"platform":"telegram","chat_id":"c1"}', "Reminder r1", "Reminder r1 is due.", {"kind": "daily", "times": ["09:00"]}, "UTC"))
            db.close()
            payload = backup_database(source, destination)
            with closing(sqlite3.connect(f"file:{destination}?mode=ro", uri=True)) as check:
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                count = check.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        self.assertEqual(payload["backup"]["integrity_check"], "ok")
        self.assertEqual(integrity, "ok")
        self.assertEqual(count, 1)

    def test_backup_command_writes_atomically_and_reports_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["REPLYLOOP_DB"] = str(Path(tmp) / "missing.db")
            result = subprocess.run([sys.executable, "-m", "replyloop", "--json", "backup", str(Path(tmp) / "backup.db")], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("database does not exist", result.stderr)

    def test_backup_rejects_live_source_destination_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            db = connect(source)
            db.add_reminder(Reminder("before", '{"platform":"telegram","chat_id":"c1"}', "Reminder before", "Reminder before is due.", {"kind": "daily", "times": ["09:00"]}, "UTC"))
            with self.assertRaisesRegex(Exception, "must not be the live source"):
                backup_database(source, Path(tmp) / "nested" / ".." / "source.db")
            db.add_reminder(Reminder("after", '{"platform":"telegram","chat_id":"c1"}', "Reminder after", "Reminder after is due.", {"kind": "daily", "times": ["10:00"]}, "UTC"))
            db.close()
            with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as check:
                reminders = [row[0] for row in check.execute("SELECT id FROM reminders ORDER BY id").fetchall()]
        self.assertEqual(reminders, ["after", "before"])

    def test_backup_rejects_existing_hardlink_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            destination = Path(tmp) / "alias.db"
            db = connect(source)
            db.close()
            os.link(source, destination)
            with self.assertRaisesRegex(Exception, "must not be the live source"):
                backup_database(source, destination)

    def test_backup_rejects_live_wal_destination_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "state.db"
            db = connect(source)
            reader = sqlite3.connect(source)
            try:
                reader.execute("BEGIN")
                reader.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
                db.add_reminder(Reminder("before", '{"platform":"telegram","chat_id":"c1"}', "Reminder before", "Reminder before is due.", {"kind": "daily", "times": ["09:00"]}, "UTC"))
                wal_path = Path(str(source) + "-wal")
                self.assertTrue(wal_path.exists())

                with self.assertRaisesRegex(Exception, "must not be the live source"):
                    backup_database(source, wal_path)

                db.add_reminder(Reminder("after", '{"platform":"telegram","chat_id":"c1"}', "Reminder after", "Reminder after is due.", {"kind": "daily", "times": ["10:00"]}, "UTC"))
            finally:
                reader.close()
                db.close()
            with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as check:
                quick = check.execute("PRAGMA quick_check").fetchone()[0]
                reminders = [row[0] for row in check.execute("SELECT id FROM reminders ORDER BY id").fetchall()]
        self.assertEqual(quick, "ok")
        self.assertEqual(reminders, ["after", "before"])

    def test_doctor_reports_corruption_without_exposing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.db"
            path.write_bytes(b"not sqlite")
            payload = doctor(path)
        self.assertFalse(payload["doctor"]["ok"])
        rendered = str(payload)
        self.assertIn("database", rendered)
        self.assertNotIn("telegram", rendered)

    def test_doctor_command_exits_nonzero_for_corrupt_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.db"
            path.write_bytes(b"not sqlite")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            env["REPLYLOOP_DB"] = str(path)
            result = subprocess.run([sys.executable, "-m", "replyloop", "--json", "doctor"], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["doctor"]["ok"])
        self.assertEqual(result.stderr, "")

    def test_doctor_reports_schema_counts_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            db = connect(path)
            db.add_reminder(Reminder("r2", '{"platform":"telegram","chat_id":"c2"}', "Reminder r2", "Reminder r2 is due.", {"kind": "daily", "times": ["09:00"]}, "UTC"))
            db.close()
            payload = doctor(path)
        self.assertTrue(payload["doctor"]["ok"])
        self.assertEqual(payload["doctor"]["counts"]["pending_reminders"], 1)
        checks = {item["name"]: item["ok"] for item in payload["doctor"]["checks"]}
        self.assertTrue(checks["schema_version"])
        self.assertTrue(checks["quick_check"])
        self.assertTrue(checks["parent_directory"])
        self.assertTrue(checks["clock_timezone"])

    def test_doctor_due_count_excludes_future_snoozes_until_boundary_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            db = connect(path)
            scheduled = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
            snoozed_until = scheduled + timedelta(hours=1)
            db.add_reminder(Reminder("r-snooze", '{"platform":"telegram","chat_id":"c1"}', "Reminder snooze", "Reminder snooze is due.", {"kind": "once", "at": "2026-01-01T09:00:00Z"}, "UTC"))
            db.add_occurrence(Occurrence("o-snooze", "r-snooze", scheduled, OccurrenceStatus.SNOOZED, due_at=snoozed_until))
            db.close()

            before = doctor(path, scheduled + timedelta(minutes=30))
            at_boundary = doctor(path, snoozed_until)

        self.assertEqual(before["doctor"]["counts"]["due"], 0)
        self.assertEqual(at_boundary["doctor"]["counts"]["due"], 1)

    def test_doctor_due_count_includes_current_due_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.db"
            db = connect(path)
            due_at = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
            db.add_reminder(Reminder("r-due", '{"platform":"telegram","chat_id":"c1"}', "Reminder due", "Reminder due is due.", {"kind": "once", "at": "2026-01-01T09:00:00Z"}, "UTC"))
            db.add_occurrence(Occurrence("o-due", "r-due", due_at, OccurrenceStatus.DUE, due_at=due_at))
            db.close()

            payload = doctor(path, due_at)

        self.assertEqual(payload["doctor"]["counts"]["due"], 1)


if __name__ == "__main__":
    unittest.main()
