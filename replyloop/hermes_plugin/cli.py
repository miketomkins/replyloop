"""Hermes CLI bridge for `hermes replyloop`."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict
from typing import Any, Callable

from replyloop import cli as standalone
from replyloop.db import connect
from replyloop.errors import MigrationError, ValidationError
from replyloop.service import ReminderService

from .delivery import HermesDeliveryAdapter


def setup_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("replyloop_args", nargs=argparse.REMAINDER, help="arguments passed to the standalone replyloop CLI")


def make_handler(ctx: Any) -> Callable[[argparse.Namespace], int]:
    def handle_command(args: argparse.Namespace) -> int:
        argv = list(getattr(args, "replyloop_args", []) or [])
        parser = standalone.build_parser()
        parsed = parser.parse_args(argv)
        if parsed.command != "tick":
            return standalone.main(argv)
        try:
            with connect(standalone.resolve_db_path(parsed.db)) as db:
                adapter = HermesDeliveryAdapter(ctx)
                result = ReminderService(db, adapter).tick()
            standalone.emit(parsed, {"tick": asdict(result)})
            return 1 if result.failed else 0
        except (ValidationError, MigrationError, sqlite3.Error, OSError, ValueError) as exc:
            return standalone.emit_error(parsed, standalone._safe_error(exc), 2)

    return handle_command


def register_cli(ctx: Any) -> None:
    ctx.register_cli_command(
        "replyloop",
        help="Manage ReplyLoop reminders",
        setup_fn=setup_parser,
        handler_fn=make_handler(ctx),
        description="Run the ReplyLoop CLI through Hermes; tick delivers through send_message.",
    )
