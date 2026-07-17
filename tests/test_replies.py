from __future__ import annotations

import unittest

from replyloop.errors import ValidationError
from replyloop.replies import ReplyCommand, ReplyIdentity, parse_duration_minutes, parse_reply, target_matches


class ReplyParserTests(unittest.TestCase):
    def test_exact_commands_are_case_insensitive(self) -> None:
        self.assertEqual(parse_reply(" done ").command, ReplyCommand.DONE)  # type: ignore[union-attr]
        self.assertEqual(parse_reply("SNOOZE").command, ReplyCommand.SNOOZE)  # type: ignore[union-attr]
        self.assertEqual(parse_reply("cancel").command, ReplyCommand.CANCEL)  # type: ignore[union-attr]
        parsed = parse_reply("snooze 2h")
        assert parsed is not None
        self.assertEqual(parsed.snooze_minutes, 120)

    def test_unrelated_and_non_exact_text_is_ignored(self) -> None:
        self.assertIsNone(parse_reply("please done"))
        self.assertIsNone(parse_reply("done now"))
        self.assertIsNone(parse_reply("snooze 1 hour"))

    def test_duration_bounds_and_units(self) -> None:
        self.assertEqual(parse_duration_minutes("15m"), 15)
        self.assertEqual(parse_duration_minutes("3h"), 180)
        self.assertEqual(parse_duration_minutes("2d"), 2880)
        for bad in ("0m", "-1m", "1w", "9999d"):
            with self.assertRaises(ValidationError):
                parse_duration_minutes(bad)

    def test_target_matches_sender_and_chat_type(self) -> None:
        target = {"platform": "telegram", "chat_id": "c1", "sender_id": "u1", "is_dm": True}
        self.assertTrue(target_matches(target, ReplyIdentity("telegram", "c1", "u1", True)))
        self.assertFalse(target_matches(target, ReplyIdentity("telegram", "c1", "u2", True)))
        self.assertFalse(target_matches(target, ReplyIdentity("telegram", "c1", "u1", False)))
        self.assertTrue(target_matches({"platform": "telegram", "chat_id": "c1"}, ReplyIdentity("telegram", "c1", None, True)))

    def test_photon_dm_target_requires_meaningful_sender_binding(self) -> None:
        identity = ReplyIdentity("photon", "c-a", "s-a", True)
        self.assertFalse(target_matches({"platform": "photon", "chat_id": "c-a", "is_dm": True}, identity))
        self.assertFalse(target_matches({"platform": "Photon", "chat_id": "c-a", "sender_id": " ", "is_dm": True}, identity))


if __name__ == "__main__":
    unittest.main()
