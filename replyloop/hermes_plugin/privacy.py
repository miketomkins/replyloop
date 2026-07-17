"""Privacy guards for ReplyLoop's Hermes gateway integration."""

from __future__ import annotations

import logging
from typing import Any

from .delivery import redacted_label

_GATEWAY_LOGGER_NAME = "gateway.run"
_SKIP_LOG_MESSAGE = "pre_gateway_dispatch skip: reason=%s platform=%s chat=%s"
_HANDLED_REASON = "replyloop-command-handled"


class ReplyLoopSkipLogFilter(logging.Filter):
    """Redact ReplyLoop handled-skip chat identifiers from Hermes gateway logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != _SKIP_LOG_MESSAGE:
            return True
        args = record.args
        if not isinstance(args, tuple) or len(args) != 3:
            return True
        reason, platform, chat = args
        if reason != _HANDLED_REASON:
            return True
        record.args = (reason, platform, redacted_label(chat))
        return True


def install_gateway_privacy_guard(logger: logging.Logger | None = None) -> bool:
    """Install the ReplyLoop skip-log redaction filter on Hermes' gateway logger.

    Returns False instead of raising so plugin registration can fail closed and
    avoid registering the pre-dispatch hook when the guard cannot be installed.
    """

    try:
        target = logger or logging.getLogger(_GATEWAY_LOGGER_NAME)
        if not any(isinstance(existing, ReplyLoopSkipLogFilter) for existing in target.filters):
            target.addFilter(ReplyLoopSkipLogFilter())
        return True
    except Exception:
        return False


def render_gateway_skip_log(*, reason: str, platform: Any, chat: Any) -> str:
    """Render a synthetic Hermes skip log through the real logging formatter path."""

    record = logging.LogRecord(
        name=_GATEWAY_LOGGER_NAME,
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=_SKIP_LOG_MESSAGE,
        args=(reason, platform, chat),
        exc_info=None,
    )
    for log_filter in logging.getLogger(_GATEWAY_LOGGER_NAME).filters:
        allowed = log_filter(record) if callable(log_filter) else log_filter.filter(record)
        if not allowed:
            return ""
    return logging.Formatter("%(message)s").format(record)
