from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


from replyloop.hermes_plugin import register
from replyloop.hermes_plugin import delivery
from replyloop.hermes_plugin.delivery import HermesDeliveryAdapter


class FakePluginContext:
    def __init__(self) -> None:
        self.tools = {}
        self.cli_commands = {}
        self.hooks = {}
        self.sends = []
        self.dispatch_result = {"success": True, "message_id": "provider-1"}
        self.dispatch_unknown = False

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands[name] = {"setup_fn": setup_fn, "handler_fn": handler_fn, "description": description}

    def register_hook(self, hook_name, callback):
        self.hooks.setdefault(hook_name, []).append(callback)

    def dispatch_tool(self, tool_name, args, **kwargs):
        self.sends.append({"tool_name": tool_name, "args": args, "kwargs": kwargs})
        if tool_name != "send_message":
            raise AssertionError(f"unexpected registry dispatch: {tool_name}")
        if self.dispatch_unknown:
            return json.dumps({"error": "Unknown tool: send_message"})
        return json.dumps(self.dispatch_result)

    def replyloop_send_message(self, args):  # pragma: no cover - legacy seam must not be used
        raise AssertionError("unexpected direct replyloop_send_message seam")


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

    def test_cli_tick_uses_dispatch_tool_send_message(self) -> None:
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
        self.assertEqual(ctx.sends[0]["tool_name"], "send_message")
        self.assertEqual(ctx.sends[0]["args"]["target"], "photon:c-a")

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
                title="Hermes custom title",
                message="Hermes custom body",
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
            attempts = db.connection.execute("SELECT status, error, provider_message_id FROM delivery_attempts ORDER BY created_at, id").fetchall()
            success_events = [
                json.loads(row["payload_json"])
                for row in db.connection.execute("SELECT payload_json FROM events WHERE event_type = ? ORDER BY id", ("delivery.succeeded",)).fetchall()
            ]
            db.close()
        self.assertEqual((second.attempted, second.delivered), (1, 1))
        self.assertEqual(len(ctx.sends), 2)
        self.assertIn("Hermes custom title", ctx.sends[-1]["args"]["message"])
        self.assertIn("Hermes custom body", ctx.sends[-1]["args"]["message"])
        self.assertIn("SNOOZE <duration>", ctx.sends[-1]["args"]["message"])
        self.assertEqual([row["status"] for row in attempts], ["failure", "success"])
        self.assertEqual([row["provider_message_id"] for row in attempts], [None, "photon-msg-1"])
        self.assertEqual(success_events[-1]["provider_message_id"], "photon-msg-1")
        self.assertNotIn(phone, attempts[0]["error"])
        self.assertNotIn("c-a", attempts[0]["error"])

    def test_dispatch_tool_send_message_success_records_provider_id(self) -> None:
        ctx = FakePluginContext()
        ctx.dispatch_result = {"success": True, "message_id": "provider-2"}
        adapter = HermesDeliveryAdapter(ctx)
        request = SimpleNamespace(
            target={"platform": "telegram", "chat_id": "12345", "sender_id": "67890"},
            text="hello",
            idempotency_key="idem-1",
        )
        outcome = adapter.deliver(request)  # type: ignore[arg-type]

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.provider_message_id, "provider-2")
        self.assertEqual(ctx.sends[0]["tool_name"], "send_message")

    def test_unknown_registry_send_message_falls_back_to_direct_helper(self) -> None:
        ctx = FakePluginContext()
        ctx.dispatch_unknown = True
        calls = []
        original = delivery._direct_send_message
        delivery._direct_send_message = lambda args: calls.append(args) or json.dumps({"success": True, "message_id": "direct-1"})
        try:
            adapter = HermesDeliveryAdapter(ctx)
            request = SimpleNamespace(
                target={"platform": "photon", "chat_id": "c-a", "sender_id": "s-a"},
                text="hello",
                idempotency_key="idem-1",
            )
            outcome = adapter.deliver(request)  # type: ignore[arg-type]
        finally:
            delivery._direct_send_message = original

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.provider_message_id, "direct-1")
        self.assertEqual(ctx.sends[0]["tool_name"], "send_message")
        self.assertEqual(calls, [{"action": "send", "target": "photon:c-a", "message": "hello"}])

    def test_handler_returns_json_error_for_malformed_args(self) -> None:
        ctx = FakePluginContext()
        register(ctx)
        result = json.loads(ctx.tools["replyloop_doctor"]["handler"]([("db", "a"), ("db", "b", "c")]))
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_create_rejects_photon_dm_target_without_sender_id(self) -> None:
        ctx = FakePluginContext()
        register(ctx)
        with tempfile.TemporaryDirectory() as tmp:
            result = json.loads(
                ctx.tools["replyloop_create"]["handler"](
                    {
                        "db": str(Path(tmp) / "state.sqlite"),
                        "id": "r1",
                        "title": "Plugin title",
                        "message": "Plugin body",
                        "schedule": {"kind": "daily", "times": ["09:00"]},
                        "target": {"platform": "photon", "chat_id": "c-a", "is_dm": True},
                        "timezone": "UTC",
                    }
                )
            )
        self.assertFalse(result["ok"])
        self.assertIn("sender", result["error"])

    def test_create_rejects_photon_dm_target_with_malformed_sender_id(self) -> None:
        ctx = FakePluginContext()
        register(ctx)
        for target in (
            {"platform": "Photon", "chat_id": "c-a", "is_dm": True},
            {"platform": "photon", "chat_id": "c-a", "sender_id": " ", "is_dm": True},
        ):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                result = json.loads(
                    ctx.tools["replyloop_create"]["handler"](
                        {
                            "db": str(Path(tmp) / "state.sqlite"),
                            "id": "r1",
                            "title": "Plugin title",
                            "message": "Plugin body",
                            "schedule": {"kind": "daily", "times": ["09:00"]},
                            "target": target,
                            "timezone": "UTC",
                        }
                    )
                )
            self.assertFalse(result["ok"])
            self.assertIn("sender", result["error"])


if __name__ == "__main__":
    unittest.main()
