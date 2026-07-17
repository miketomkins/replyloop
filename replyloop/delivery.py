"""Delivery adapter protocols and structured transport outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OutcomeStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class DeliveryRequest:
    occurrence_id: str
    reminder_id: str
    target: dict[str, str | bool | None]
    text: str


@dataclass(frozen=True)
class DeliveryOutcome:
    status: OutcomeStatus
    transport: str
    provider_message_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, OutcomeStatus):
            raise ValueError("status must be an OutcomeStatus")
        if not isinstance(self.transport, str) or not self.transport:
            raise ValueError("transport is required")
        if self.status == OutcomeStatus.SUCCESS:
            if not isinstance(self.provider_message_id, str) or not self.provider_message_id:
                raise ValueError("provider_message_id is required")
            if self.error is not None:
                raise ValueError("successful outcome cannot include error")
        else:
            if not isinstance(self.error, str) or not self.error:
                raise ValueError("error is required")
            if self.provider_message_id is not None:
                raise ValueError("failed outcome cannot include provider_message_id")

    @classmethod
    def success(cls, transport: str, provider_message_id: str) -> DeliveryOutcome:
        return cls(OutcomeStatus.SUCCESS, transport, provider_message_id=provider_message_id)

    @classmethod
    def failure(cls, transport: str, error: str) -> DeliveryOutcome:
        return cls(OutcomeStatus.FAILURE, transport, error=error)


class DeliveryAdapter(Protocol):
    transport: str

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        """Attempt delivery and return a structured outcome without raising for transport failure."""
        ...


class RecordingAdapter:
    """Synthetic adapter used by tests."""

    transport = "synthetic"

    def __init__(self, outcomes: list[DeliveryOutcome] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.requests: list[DeliveryRequest] = []
        self._next = 1

    def deliver(self, request: DeliveryRequest) -> DeliveryOutcome:
        self.requests.append(request)
        if self.outcomes:
            return self.outcomes.pop(0)
        message_id = f"msg-{self._next}"
        self._next += 1
        return DeliveryOutcome.success(self.transport, message_id)
