from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from replyloop.cli import backup_database, doctor
from replyloop.db import connect
from replyloop.models import Reminder

ROOT = Path(__file__).resolve().parents[1]


class BackupTests(unittest.TestCase):
    def test_backup_uses_reopenable_integrity_checked_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.db"
            destination = Path(tmp) / "copy.db"
            db = connect(source)
            db.add_reminder(Reminder("r1", '{"platform":"telegram","chat_id":"c1"}', {"kind": "daily", "times": ["09:00"]}, "UTC"))
            db.close()
            payload = backup_database(source, destination)
            with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as check:
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
            db.add_reminder(Reminder("r2", '{"platform":"telegram","chat_id":"c2"}', {"kind": "daily", "times": ["09:00"]}, "UTC"))
            db.close()
            payload = doctor(path)
        self.assertTrue(payload["doctor"]["ok"])
        self.assertEqual(payload["doctor"]["counts"]["pending_reminders"], 1)
        checks = {item["name"]: item["ok"] for item in payload["doctor"]["checks"]}
        self.assertTrue(checks["schema_version"])
        self.assertTrue(checks["quick_check"])
        self.assertTrue(checks["parent_directory"])
        self.assertTrue(checks["clock_timezone"])


if __name__ == "__main__":
    unittest.main()
