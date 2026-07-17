"""Standalone ReplyLoop command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import ReplyLoopDB, connect
from .delivery import DeliveryOutcome, DeliveryRequest
from .errors import MigrationError, ValidationError
from .models import Event, OccurrenceStatus, ReminderStatus, datetime_to_iso, utc_now
from .replies import ReplyIdentity
from .service import ReminderService

EXPECTED_SCHEMA_VERSION = "004_reminder_content_and_receipts"


class CLIError(Exception):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class CommandResult:
    payload: dict[str, Any]
    exit_code: int = 0


class StdoutAdapter:
    transport = "stdout"

    def __init__(self, *, fail: bool = False, print_deliveries: bool = True) -> None:
        self.fail = fail
        self.print_deliveries = print_deliveries
        self.requests: list[DeliveryRequest] = []
        self.delivery_records: list[dict[str, Any]] = []

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        self.requests.append(request)
        safe = {
            "occurrence_id": request.occurrence_id,
            "reminder_id": request.reminder_id,
            "idempotency_key": request.idempotency_key,
            "transport": self.transport,
            "text": request.text,
        }
        self.delivery_records.append(safe)
        if self.print_deliveries:
            print(json.dumps({"delivery": safe}, sort_keys=True))
        if self.fail:
            return DeliveryOutcome.failure(self.transport, "forced failure")
        return DeliveryOutcome.success(self.transport, f"stdout-{len(self.requests)}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except CLIError as exc:
        return emit_error(args, str(exc), exc.exit_code)
    except (ValidationError, MigrationError, sqlite3.Error, OSError, ValueError) as exc:
        return emit_error(args, _safe_error(exc), 2)
    emit(args, result.payload)
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="replyloop", description="Manage local ReplyLoop reminders")
    parser.add_argument("--db", help="SQLite database path. Defaults to REPLYLOOP_DB or XDG data home.")
    parser.add_argument("--json", action="store_true", help="write JSON output")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", parents=[common], help="create a reminder")
    create.add_argument("--id", dest="reminder_id", help="stable reminder id. Defaults to a generated id.")
    create.add_argument("--title", required=True, help="user-visible reminder title")
    create.add_argument("--message", required=True, help="user-visible reminder message")
    create.add_argument("--schedule-json", help="explicit schedule JSON object")
    create.add_argument("--once-at", help="create a once schedule at an ISO datetime")
    create.add_argument("--daily", action="store_true", help="create a daily schedule")
    create.add_argument("--weekly", action="store_true", help="create a weekly schedule")
    create.add_argument("--time", action="append", dest="times", help="HH:MM local time. Repeat for multiple times.")
    create.add_argument("--weekday", action="append", type=int, dest="weekdays", help="weekly day, Monday=0 through Sunday=6. Repeat as needed.")
    create.add_argument("--timezone", default="UTC", help="IANA timezone, default UTC")
    create.add_argument("--target", help="target JSON object")
    create.add_argument("--platform", help="target platform")
    create.add_argument("--chat", dest="chat_id", help="target chat identifier")
    create.add_argument("--sender", dest="sender_id", help="target sender identifier")
    create.add_argument("--chat-type", choices=("dm", "group"), default="dm", help="reply matching chat type")
    create.add_argument("--snooze", type=int, default=60, help="default snooze minutes")
    create.add_argument("--escalation", action="append", type=int, default=[], help="escalation interval in minutes. Repeat as needed.")
    create.add_argument("--max-deliveries", type=int, default=1, help="maximum delivery count including escalations")
    create.add_argument("--repeat-last", action="store_true", help="repeat the last escalation interval")

    for name in ("list", "show", "pause", "resume", "cancel"):
        cmd = sub.add_parser(name, parents=[common], help=f"{name} reminders")
        if name == "list":
            cmd.add_argument("--status", choices=[item.value for item in ReminderStatus], help="filter by status")
        else:
            cmd.add_argument("reminder_id")

    tick = sub.add_parser("tick", parents=[common], help="create and deliver due reminders")
    tick.add_argument("--fail", action="store_true", help="force the deterministic adapter to fail deliveries")

    reply = sub.add_parser("reply", parents=[common], help="process a deterministic local reply")
    reply.add_argument("--platform", required=True)
    reply.add_argument("--chat", required=True, dest="chat_id")
    reply.add_argument("--sender", dest="sender_id")
    reply.add_argument("--chat-type", choices=("dm", "group"), default="dm")
    reply.add_argument("text")

    backup = sub.add_parser("backup", parents=[common], help="create an integrity-checked SQLite backup")
    backup.add_argument("destination")

    sub.add_parser("doctor", parents=[common], help="run operational diagnostics")
    return parser


def run(args: argparse.Namespace) -> CommandResult:
    db_path = resolve_db_path(args.db)
    if args.command == "backup":
        return CommandResult(backup_database(db_path, Path(args.destination)))
    if args.command == "doctor":
        payload = doctor(db_path)
        return CommandResult(payload, 0 if payload["doctor"]["ok"] else 1)
    with connect_for_command(db_path) as db:
        if args.command == "create":
            return CommandResult(create_reminder(db, args))
        if args.command == "list":
            return CommandResult({"reminders": [row_to_public_reminder(row) for row in list_reminder_rows(db, args.status)]})
        if args.command == "show":
            return CommandResult(show_reminder(db, args.reminder_id))
        if args.command in {"pause", "resume", "cancel"}:
            return CommandResult(set_status(db, args.reminder_id, args.command))
        if args.command == "tick":
            adapter = StdoutAdapter(fail=args.fail, print_deliveries=not getattr(args, "json", False))
            result = ReminderService(db, adapter).tick()
            exit_code = 1 if result.failed else 0
            return CommandResult({"tick": asdict(result), "deliveries": adapter.delivery_records}, exit_code)
        if args.command == "reply":
            identity = ReplyIdentity(args.platform, args.chat_id, args.sender_id, args.chat_type == "dm")
            result = ReminderService(db, StdoutAdapter()).handle_reply(args.text, identity)
            payload = {"reply": {"handled": result.handled, "command": result.command.value if result.command else None, "occurrence_id": result.occurrence_id, "reason": result.reason}}
            return CommandResult(payload, 0 if result.handled else 1)
    raise CLIError("unknown command")


def resolve_db_path(explicit: str | None = None) -> Path:
    value = explicit or os.environ.get("REPLYLOOP_DB")
    if value:
        return Path(value).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "replyloop" / "replyloop.db"


def connect_for_command(path: Path) -> ReplyLoopDB:
    path.parent.mkdir(parents=True, exist_ok=True)
    return connect(path)


def create_reminder(db: ReplyLoopDB, args: argparse.Namespace) -> dict[str, Any]:
    schedule = parse_schedule_args(args)
    target = parse_target_args(args)
    reminder_id = args.reminder_id or f"rem_{uuid.uuid4().hex[:12]}"
    reminder = ReminderService(db, StdoutAdapter()).create_reminder(
        reminder_id=reminder_id,
        target=target,
        schedule=schedule,
        timezone=args.timezone,
        title=args.title,
        message=args.message,
        default_snooze_minutes=args.snooze,
        intervals_minutes=tuple(args.escalation),
        max_deliveries=args.max_deliveries,
        repeat_last=args.repeat_last,
    )
    return {"reminder": row_to_public_reminder(db.connection.execute("SELECT * FROM reminders WHERE id = ?", (reminder.id,)).fetchone())}


def parse_schedule_args(args: argparse.Namespace) -> dict[str, Any]:
    modes = [bool(args.schedule_json), bool(args.once_at), bool(args.daily), bool(args.weekly)]
    if sum(modes) != 1:
        raise CLIError("choose exactly one schedule mode: --schedule-json, --once-at, --daily, or --weekly")
    if args.schedule_json:
        return parse_json_object(args.schedule_json, "schedule-json")
    if args.once_at:
        return {"kind": "once", "at": args.once_at}
    if args.daily:
        if not args.times:
            raise CLIError("--daily requires at least one --time HH:MM")
        return {"kind": "daily", "times": args.times}
    if not args.times:
        raise CLIError("--weekly requires at least one --time HH:MM")
    if not args.weekdays:
        raise CLIError("--weekly requires at least one --weekday 0-6")
    return {"kind": "weekly", "times": args.times, "weekdays": args.weekdays}


def parse_target_args(args: argparse.Namespace) -> dict[str, Any]:
    if args.target:
        return parse_json_object(args.target, "target")
    if not args.platform or not args.chat_id:
        raise CLIError("target requires either --target JSON or both --platform and --chat")
    target: dict[str, Any] = {"platform": args.platform, "chat_id": args.chat_id, "is_dm": args.chat_type == "dm"}
    if args.sender_id:
        target["sender_id"] = args.sender_id
    return target


def parse_json_object(text: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CLIError(f"{name} must be valid JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CLIError(f"{name} must be a JSON object")
    return value


def list_reminder_rows(db: ReplyLoopDB, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return list(db.connection.execute("SELECT * FROM reminders WHERE status = ? ORDER BY created_at, id", (status,)).fetchall())
    return list(db.connection.execute("SELECT * FROM reminders ORDER BY created_at, id").fetchall())


def show_reminder(db: ReplyLoopDB, reminder_id: str) -> dict[str, Any]:
    row = db.connection.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if row is None:
        raise CLIError("reminder not found", 1)
    occurrences = [row_to_public_occurrence(item) for item in db.connection.execute("SELECT * FROM occurrences WHERE reminder_id = ? ORDER BY scheduled_for, id", (reminder_id,)).fetchall()]
    return {"reminder": row_to_public_reminder(row), "occurrences": occurrences}


def set_status(db: ReplyLoopDB, reminder_id: str, command: str) -> dict[str, Any]:
    status = {"pause": ReminderStatus.PAUSED, "resume": ReminderStatus.ACTIVE, "cancel": ReminderStatus.CANCELLED}[command]
    event = {"pause": "reminder.paused", "resume": "reminder.resumed", "cancel": "reminder.cancelled"}[command]
    row = db.connection.execute("SELECT status FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    if row is None:
        raise CLIError("reminder not found", 1)
    current = ReminderStatus(row["status"])
    allowed = {
        "pause": {ReminderStatus.ACTIVE},
        "resume": {ReminderStatus.PAUSED},
        "cancel": {ReminderStatus.ACTIVE, ReminderStatus.PAUSED},
    }
    if current not in allowed[command]:
        raise CLIError(f"cannot {command} reminder in {current.value} status", 1)
    now = utc_now()
    with db.transaction() as connection:
        cursor = connection.execute(
            "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
            (status.value, datetime_to_iso(now), reminder_id, current.value),
        )
        if cursor.rowcount != 1:
            raise CLIError("reminder status changed; retry command", 1)
        _insert_event(connection, Event(None, "reminder", reminder_id, event, {"status": status.value}, now))
        if status == ReminderStatus.CANCELLED:
            rows = connection.execute(
                "SELECT id FROM occurrences WHERE reminder_id = ? AND status IN (?, ?, ?, ?)",
                (
                    reminder_id,
                    OccurrenceStatus.DUE.value,
                    OccurrenceStatus.DELIVERING.value,
                    OccurrenceStatus.DELIVERED.value,
                    OccurrenceStatus.SNOOZED.value,
                ),
            ).fetchall()
            for occurrence in rows:
                connection.execute(
                    "UPDATE occurrences SET status = ?, updated_at = ?, delivery_claim_id = NULL WHERE id = ?",
                    (OccurrenceStatus.CANCELLED.value, datetime_to_iso(now), occurrence["id"]),
                )
                _insert_event(connection, Event(None, "occurrence", occurrence["id"], "occurrence.cancelled", {"reminder_id": reminder_id}, now))
    return show_reminder(db, reminder_id)


def backup_database(source: Path, destination: Path) -> dict[str, Any]:
    if not source.exists():
        raise CLIError("database does not exist", 1)
    source_path = source.expanduser().resolve(strict=True)
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_path = destination.resolve(strict=False)
    if _is_live_sqlite_path(source_path, destination, destination_path):
        raise CLIError("backup destination must not be the live source database", 1)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(tmp_path)) as dst:
            src.backup(dst)
        with closing(sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CLIError("backup integrity_check failed", 1)
        os.replace(tmp_path, destination)
        return {"backup": {"path": str(destination), "integrity_check": integrity}}
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _is_live_sqlite_path(source_path: Path, destination: Path, destination_path: Path) -> bool:
    live_paths = [source_path, *(Path(f"{source_path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))]
    for live_path in live_paths:
        if live_path.resolve(strict=False) == destination_path:
            return True
        if live_path.exists() and destination.exists() and os.path.samefile(live_path, destination):
            return True
    return False


def doctor(path: Path, now_utc: datetime | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    now = now_utc or datetime.now(UTC)
    due_boundary = now + timedelta(microseconds=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    checks.append(check_item("parent_directory", path.parent.is_dir() and os.access(path.parent, os.R_OK | os.W_OK | os.X_OK), str(path.parent)))
    checks.append(check_timezone())
    try:
        with connect(path) as db:
            versions = [row["version"] for row in db.connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
            current = versions[-1] if versions else None
            checks.append(check_item("schema_version", current == EXPECTED_SCHEMA_VERSION, current or "missing"))
            quick = db.connection.execute("PRAGMA quick_check").fetchone()[0]
            checks.append(check_item("quick_check", quick == "ok", quick))
            due_count = db.count_due_occurrences(due_boundary)
            pending_count = db.connection.execute("SELECT COUNT(*) FROM reminders WHERE status = 'active'").fetchone()[0]
            retry_count = db.connection.execute(
                """
                SELECT COUNT(DISTINCT f.occurrence_id)
                FROM delivery_attempts f
                JOIN occurrences o ON o.id = f.occurrence_id
                WHERE f.status = 'failure'
                  AND f.applied_to_occurrence = 1
                  AND o.status IN ('due', 'snoozed', 'delivering')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM delivery_attempts s
                      WHERE s.occurrence_id = f.occurrence_id
                        AND s.status = 'success'
                        AND s.applied_to_occurrence = 1
                        AND (s.attempted_at > f.attempted_at OR (s.attempted_at = f.attempted_at AND s.id > f.id))
                  )
                """
            ).fetchone()[0]
    except sqlite3.DatabaseError as exc:
        checks.append(check_item("database", False, _safe_error(exc)))
        due_count = pending_count = retry_count = None
    ok = all(item["ok"] for item in checks)
    return {"doctor": {"ok": ok, "database_path": str(path), "checks": checks, "counts": {"due": due_count, "pending_reminders": pending_count, "retry_queue": retry_count}}}


def check_item(name: str, ok: bool, detail: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "ok": bool(ok)}
    if detail is not None:
        item["detail"] = detail
    return item


def check_timezone() -> dict[str, Any]:
    try:
        ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError) as exc:
        return check_item("timezone", False, _safe_error(exc))
    now = datetime.now(UTC)
    return check_item("clock_timezone", now.tzinfo is not None, datetime_to_iso(now))


def _insert_event(connection: sqlite3.Connection, event: Event) -> None:
    connection.execute(
        "INSERT INTO events(aggregate_type, aggregate_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (event.aggregate_type, event.aggregate_id, event.event_type, json.dumps(event.payload, sort_keys=True, separators=(",", ":")), datetime_to_iso(event.created_at)),
    )


def row_to_public_reminder(row: sqlite3.Row) -> dict[str, Any]:
    if row is None:
        raise CLIError("reminder not found", 1)
    return {
        "id": row["id"],
        "title": row["title"],
        "message": row["message"],
        "schedule": json.loads(row["schedule_json"]),
        "timezone": row["timezone"],
        "status": row["status"],
        "default_snooze_minutes": row["default_snooze_minutes"],
        "escalation_minutes": json.loads(row["escalation_minutes_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def row_to_public_occurrence(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "reminder_id": row["reminder_id"], "scheduled_for": row["scheduled_for"], "due_at": row["due_at"], "status": row["status"]}


def emit(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def emit_error(args: argparse.Namespace, message: str, exit_code: int) -> int:
    payload = {"error": {"message": message}}
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
    else:
        print(f"error: {message}", file=sys.stderr)
    return exit_code


def _safe_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return text.replace(os.environ.get("REPLYLOOP_DB", "\0"), "[database]")


if __name__ == "__main__":
    raise SystemExit(main())
