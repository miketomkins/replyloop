from __future__ import annotations

import unittest

from replyloop.delivery import DeliveryOutcome, DeliveryRequest, OutcomeStatus, RecordingAdapter


class DeliveryTests(unittest.TestCase):
    def test_outcomes_validate_required_fields(self) -> None:
        self.assertEqual(DeliveryOutcome.success("telegram", "m1").status, OutcomeStatus.SUCCESS)
        self.assertEqual(DeliveryOutcome.failure("telegram", "offline").status, OutcomeStatus.FAILURE)
        with self.assertRaises(ValueError):
            DeliveryOutcome.success("telegram", "")
        with self.assertRaises(ValueError):
            DeliveryOutcome.failure("telegram", "")
        with self.assertRaises(ValueError):
            DeliveryOutcome(OutcomeStatus.SUCCESS, "telegram")
        with self.assertRaises(ValueError):
            DeliveryOutcome(OutcomeStatus.FAILURE, "telegram", provider_message_id="m1")

    def test_recording_adapter_returns_queued_outcomes_then_successes(self) -> None:
        adapter = RecordingAdapter([DeliveryOutcome.failure("synthetic", "offline")])
        request = DeliveryRequest("o1", "r1", {"platform": "telegram", "chat_id": "c1", "is_dm": True}, "body")
        self.assertEqual(adapter.deliver(request).status, OutcomeStatus.FAILURE)
        self.assertEqual(adapter.deliver(request).provider_message_id, "msg-1")
        self.assertEqual(adapter.requests, [request, request])


if __name__ == "__main__":
    unittest.main()
