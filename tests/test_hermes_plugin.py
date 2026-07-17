from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from replyloop.hermes_plugin import register
from replyloop.hermes_plugin.delivery import HermesDeliveryAdapter


class FakePluginContext:
    def __init__(self) -> None:
        self.tools = {}
        self.cli_commands = {}
        self.hooks = {}
        self.dispatches = []
        self.dispatch_result = {"success": True, "message_id": "provider-1"}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn, "description": description}

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.dispatches.append((tool_name, args, kwargs))
        return json.dumps(self.dispatch_result)


class HermesPluginRegistrationTests(unittest.TestCase):
    def test_registers_tools_cli_and_gateway_hook_without_hermes_import(self) -> None:
        ctx = FakePluginContext()
        register(ctx)
        self.assertEqual(
            set(ctx.tools),
            {
                "replyloop_create",
                "replyloop_list",
                "replyloop_get",
                "replyloop_pause",
                "replyloop_resume",
                "replyloop_cancel",
                "replyloop_tick",
                "replyloop_doctor",
            },
        )
        self.assertIn("replyloop", ctx.cli_commands)
        self.assertEqual(len(ctx.hooks["pre_gateway_dispatch"]), 1)
        result = json.loads(ctx.tools["replyloop_doctor"]["handler"]({"db": str(Path(tempfile.gettempdir()) / "replyloop-plugin-doctor.sqlite")}))
        self.assertIn("ok", result)

    def test_cli_tick_uses_hermes_send_message_dispatch(self) -> None:
        from datetime import datetime, timezone
        from replyloop.clock import FakeClock
        from replyloop.db import connect
        from replyloop.delivery import RecordingAdapter
        from replyloop.service import ReminderService

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            db = connect(db_path)
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc))
            ReminderService(db, RecordingAdapter(), clock).create_reminder(
                reminder_id="r1",
                target={"platform": "photon", "chat_id": "c-a", "sender_id": "s-a", "is_dm": True},
                schedule={"kind": "once", "at": "2026-01-01T09:00:00Z"},
                timezone="UTC",
            )
            db.close()
            ctx = FakePluginContext()
            register(ctx)
            handler = ctx.cli_commands["replyloop"]["handler_fn"]
            code = handler(argparse.Namespace(replyloop_args=["--db", str(db_path), "--json", "tick"]))
        self.assertEqual(code, 0)
        self.assertEqual(ctx.dispatches[0][0], "send_message")
        self.assertEqual(ctx.dispatches[0][1]["target"], "photon:c-a")

    def test_outage_remains_pending_and_recovery_sends_once(self) -> None:
        from datetime import datetime, timedelta, timezone
        from replyloop.clock import FakeClock
        from replyloop.db import connect
        from replyloop.delivery import RecordingAdapter
        from replyloop.models import OccurrenceStatus
        from replyloop.service import ReminderService

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite"
            clock = FakeClock(datetime(2026, 1, 1, 8, 59, tzinfo=timezone.utc))
            setup_db = connect(db_path)
            ReminderService(setup_db, RecordingAdapter(), clock).create_reminder(
                reminder_id="r1",
                target={"platform": "photon", "chat_id": "c-a", "sender_id": "s-a", "is_dm": True},
                schedule={"kind": "daily", "times": ["09:00"]},
                timezone="UTC",
            )
            setup_db.close()
            clock.set(datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc))
            ctx = FakePluginContext()
            phone = "+" + "15551234567"
            ctx.dispatch_result = {"success": False, "error": "Photon offline " + phone}
            db = connect(db_path)
            first = ReminderService(db, HermesDeliveryAdapter(ctx), clock).tick()
            occurrence = db.connection.execute("SELECT status FROM occurrences").fetchone()["status"]
            db.close()
            self.assertEqual((first.attempted, first.failed), (1, 1))
            self.assertEqual(occurrence, OccurrenceStatus.DUE.value)

            clock.set(clock.now() + timedelta(minutes=1))
            ctx.dispatch_result = {"success": True, "message_id": "photon-msg-1"}
            db = connect(db_path)
            second = ReminderService(db, HermesDeliveryAdapter(ctx), clock).tick()
            attempts = db.connection.execute("SELECT status, error FROM delivery_attempts ORDER BY created_at, id").fetchall()
            db.close()
        self.assertEqual((second.attempted, second.delivered), (1, 1))
        self.assertEqual(len(ctx.dispatches), 2)
        self.assertEqual([row["status"] for row in attempts], ["failure", "success"])
        self.assertNotIn(phone, attempts[0]["error"])


if __name__ == "__main__":
    unittest.main()
