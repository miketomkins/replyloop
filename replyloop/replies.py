"""Exact reply command parsing and target identity helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
import re

from .errors import ValidationError

_DURATION_RE = re.compile(r"^(?P<value>[1-9][0-9]{0,3})(?P<unit>[mhd])$", re.IGNORECASE | re.ASCII)
_MAX_SNOOZE_MINUTES = 366 * 24 * 60


class ReplyCommand(StrEnum):
    DONE = "done"
    SNOOZE = "snooze"
    CANCEL = "cancel"


@dataclass(frozen=True)
class ParsedReply:
    command: ReplyCommand
    snooze_minutes: int | None = None


@dataclass(frozen=True)
class ReplyIdentity:
    platform: str
    chat_id: str
    sender_id: str | None = None
    is_dm: bool = True


def parse_reply(text: str) -> ParsedReply | None:
    stripped = text.strip()
    upper = stripped.upper()
    if upper == "DONE":
        return ParsedReply(ReplyCommand.DONE)
    if upper == "CANCEL":
        return ParsedReply(ReplyCommand.CANCEL)
    if upper == "SNOOZE":
        return ParsedReply(ReplyCommand.SNOOZE)
    parts = stripped.split()
    if len(parts) == 2 and parts[0].upper() == "SNOOZE":
        return ParsedReply(ReplyCommand.SNOOZE, parse_duration_minutes(parts[1]))
    return None


def parse_duration_minutes(text: str) -> int:
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None:
        raise ValidationError("duration must be a positive integer followed by m, h, or d")
    value = int(match.group("value"))
    unit = match.group("unit").lower()
    multiplier = {"m": 1, "h": 60, "d": 24 * 60}[unit]
    minutes = value * multiplier
    if minutes <= 0 or minutes > _MAX_SNOOZE_MINUTES:
        raise ValidationError("duration is out of bounds")
    return minutes


def duration_delta(minutes: int) -> timedelta:
    if minutes <= 0 or minutes > _MAX_SNOOZE_MINUTES:
        raise ValidationError("duration is out of bounds")
    return timedelta(minutes=minutes)


def target_matches(target: dict[str, object], identity: ReplyIdentity) -> bool:
    if target.get("platform") != identity.platform:
        return False
    if str(target.get("chat_id")) != identity.chat_id:
        return False
    target_is_dm = bool(target.get("is_dm", True))
    if target_is_dm != identity.is_dm:
        return False
    target_sender = target.get("sender_id")
    if identity.platform == "photon" and identity.is_dm:
        if not isinstance(target_sender, str) or not target_sender:
            return False
    if target_sender is not None and str(target_sender) != (identity.sender_id or ""):
        return False
    return True
