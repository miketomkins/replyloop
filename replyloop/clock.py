"""Injectable clocks for deterministic reminder lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import to_utc

UTC = timezone.utc


class Clock(Protocol):
    """Clock protocol used by services instead of reading wall time directly."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        ...


@dataclass(frozen=True)
class RealClock:
    """Production clock backed by datetime.now(timezone.utc)."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass
class FakeClock:
    """Mutable clock for offline and race tests."""

    current: datetime

    def __post_init__(self) -> None:
        self.current = to_utc(self.current)

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        self.current = to_utc(value)

    def advance(self, **kwargs: int | float) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current
