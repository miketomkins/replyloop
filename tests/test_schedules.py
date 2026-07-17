from __future__ import annotations

import unittest
from datetime import datetime, timezone

from replyloop.errors import ValidationError
from replyloop.schedules import due_times_between, validate_schedule

UTC = timezone.utc


class ScheduleValidationTests(unittest.TestCase):
    def test_daily_requires_unique_hhmm_times(self) -> None:
        schedule = validate_schedule({"kind": "daily", "times": ["14:00", "08:30"]}, "America/New_York")
        self.assertEqual([item.strftime("%H:%M") for item in schedule.times], ["08:30", "14:00"])

        invalid = [
            {"kind": "daily", "times": []},
            {"kind": "daily", "times": ["8:00"]},
            {"kind": "daily", "times": ["08:00", "08:00"]},
            {"kind": "daily", "times": ["24:00"]},
            {"kind": "daily", "times": ["08:00"], "extra": True},
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValidationError):
                    validate_schedule(candidate, "America/New_York")

    def test_weekly_requires_unique_valid_weekdays(self) -> None:
        schedule = validate_schedule({"kind": "weekly", "weekdays": [6, 0, 2], "times": ["09:00"]}, "UTC")
        self.assertEqual(schedule.weekdays, (0, 2, 6))

        invalid = [
            {"kind": "weekly", "weekdays": [], "times": ["09:00"]},
            {"kind": "weekly", "weekdays": [0, 0], "times": ["09:00"]},
            {"kind": "weekly", "weekdays": [-1], "times": ["09:00"]},
            {"kind": "weekly", "weekdays": [7], "times": ["09:00"]},
            {"kind": "weekly", "weekdays": [True], "times": ["09:00"]},
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate):
                with self.assertRaises(ValidationError):
                    validate_schedule(candidate, "UTC")

    def test_once_accepts_aware_iso_and_validates_timezone(self) -> None:
        schedule = validate_schedule({"kind": "once", "at": "2026-07-20T09:00:00-07:00"}, "America/Los_Angeles")
        assert schedule.at is not None
        self.assertEqual(schedule.at.astimezone(UTC), datetime(2026, 7, 20, 16, 0, tzinfo=UTC))
        with self.assertRaises(ValidationError):
            validate_schedule({"kind": "daily", "times": ["09:00"]}, "Missing/Zone")


class DueTimeGenerationTests(unittest.TestCase):
    def test_daily_multiple_times_inclusive_start_exclusive_end(self) -> None:
        due = due_times_between(
            {"kind": "daily", "times": ["08:00", "20:00"]},
            "UTC",
            datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
            datetime(2026, 1, 2, 8, 0, tzinfo=UTC),
        )
        self.assertEqual(due, [datetime(2026, 1, 1, 8, 0, tzinfo=UTC), datetime(2026, 1, 1, 20, 0, tzinfo=UTC)])

    def test_weekly_filters_by_local_weekday(self) -> None:
        due = due_times_between(
            {"kind": "weekly", "weekdays": [0, 2], "times": ["09:00"]},
            "UTC",
            datetime(2026, 1, 5, 0, 0, tzinfo=UTC),
            datetime(2026, 1, 8, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(due, [datetime(2026, 1, 5, 9, 0, tzinfo=UTC), datetime(2026, 1, 7, 9, 0, tzinfo=UTC)])

    def test_once_schedule_emits_only_in_window(self) -> None:
        schedule = {"kind": "once", "at": "2026-07-20T09:00:00-07:00"}
        self.assertEqual(
            due_times_between(schedule, "America/Los_Angeles", datetime(2026, 7, 20, 16, 0, tzinfo=UTC), datetime(2026, 7, 20, 16, 1, tzinfo=UTC)),
            [datetime(2026, 7, 20, 16, 0, tzinfo=UTC)],
        )
        self.assertEqual(
            due_times_between(schedule, "America/Los_Angeles", datetime(2026, 7, 20, 16, 1, tzinfo=UTC), datetime(2026, 7, 20, 17, 0, tzinfo=UTC)),
            [],
        )

    def test_spring_dst_gap_is_skipped(self) -> None:
        # 02:30 does not exist in New York on 2026-03-08, so ReplyLoop skips it instead of shifting wall time.
        due = due_times_between(
            {"kind": "daily", "times": ["02:30"]},
            "America/New_York",
            datetime(2026, 3, 8, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 9, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(due, [])

    def test_fall_dst_ambiguity_emits_both_instants(self) -> None:
        # 01:30 occurs twice in New York on 2026-11-01, and ReplyLoop emits both UTC instants deterministically.
        due = due_times_between(
            {"kind": "daily", "times": ["01:30"]},
            "America/New_York",
            datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(due, [datetime(2026, 11, 1, 5, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)])


if __name__ == "__main__":
    unittest.main()
