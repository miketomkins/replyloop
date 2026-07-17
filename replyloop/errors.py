"""ReplyLoop domain exceptions."""

from __future__ import annotations


class ReplyLoopError(Exception):
    """Base class for ReplyLoop errors."""


class ValidationError(ReplyLoopError, ValueError):
    """Raised when user supplied reminder configuration is invalid."""


class MigrationError(ReplyLoopError):
    """Raised when the database schema cannot be migrated."""


class StorageError(ReplyLoopError):
    """Raised when a storage operation cannot be completed."""
