"""Optional Hermes plugin for ReplyLoop."""

from __future__ import annotations

from typing import Any

from . import schemas
from .cli import register_cli
from .hooks import pre_gateway_dispatch
from .tools import cancel, create, doctor, get, list_reminders, make_handler, pause, resume, tick

TOOL_HANDLERS = {
    "replyloop_create": (schemas.CREATE_SCHEMA, create, "Create a ReplyLoop reminder."),
    "replyloop_list": (schemas.LIST_SCHEMA, list_reminders, "List ReplyLoop reminders."),
    "replyloop_get": (schemas.GET_SCHEMA, get, "Get a ReplyLoop reminder."),
    "replyloop_pause": (schemas.status_schema("replyloop_pause", "Pause a ReplyLoop reminder."), pause, "Pause a ReplyLoop reminder."),
    "replyloop_resume": (schemas.status_schema("replyloop_resume", "Resume a ReplyLoop reminder."), resume, "Resume a ReplyLoop reminder."),
    "replyloop_cancel": (schemas.status_schema("replyloop_cancel", "Cancel a ReplyLoop reminder."), cancel, "Cancel a ReplyLoop reminder."),
    "replyloop_tick": (schemas.TICK_SCHEMA, tick, "Deliver due ReplyLoop reminders through Hermes."),
    "replyloop_doctor": (schemas.DOCTOR_SCHEMA, doctor, "Run ReplyLoop diagnostics."),
}


def register(ctx: Any) -> None:
    """Register ReplyLoop tools, CLI, and gateway hook with Hermes."""

    for name, (schema, fn, description) in TOOL_HANDLERS.items():
        ctx.register_tool(
            name=name,
            toolset=schemas.TOOLSET,
            schema=schema,
            handler=make_handler(fn, ctx),
            description=description,
        )
    register_cli(ctx)
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch)


__all__ = ["register", "pre_gateway_dispatch"]
