"""Tests for the automatic commit message builder."""

from __future__ import annotations

import unittest
from datetime import datetime

from gitsync.commit_message import build_commit_message, default_message

MOMENT = datetime(2026, 9, 28, 9, 5, 7)


class CommitMessageTests(unittest.TestCase):
    def test_timestamp_mode_is_the_default(self) -> None:
        settings = {}
        self.assertEqual(
            build_commit_message(settings, ["Rack.FCStd"], moment=MOMENT),
            "Auto-save 2026-09-28 09:05:07 - Rack.FCStd",
        )

    def test_timestamp_mode_reports_several_documents(self) -> None:
        settings = {"auto_commit_message": "timestamp"}
        self.assertEqual(
            build_commit_message(settings, ["a.FCStd", "b.step"], moment=MOMENT),
            "Auto-save 2026-09-28 09:05:07 - 2 documents",
        )

    def test_custom_message_supports_placeholders(self) -> None:
        settings = {
            "auto_commit_message": "custom",
            "custom_commit_message": "Workshop {filename} ({count}) at {time}",
        }
        self.assertEqual(
            build_commit_message(settings, ["Rack.FCStd"], moment=MOMENT),
            "Workshop Rack.FCStd (1) at 09:05:07",
        )
        self.assertEqual(
            build_commit_message(settings, ["a.FCStd", "b.FCStd"], moment=MOMENT),
            "Workshop 2 documents (2) at 09:05:07",
        )

    def test_custom_message_can_use_date_and_stem(self) -> None:
        settings = {
            "auto_commit_message": "custom",
            "custom_commit_message": "{date} {name} saved",
        }
        self.assertEqual(
            build_commit_message(settings, ["Sub/Rack.FCStd"], moment=MOMENT),
            "2026-09-28 Rack saved",
        )

    def test_custom_message_ignores_unknown_placeholder(self) -> None:
        settings = {
            "auto_commit_message": "custom",
            "custom_commit_message": "Save {filename} at {timestamp} for {owner}",
        }
        message = build_commit_message(settings, ["Rack.FCStd"], moment=MOMENT)
        self.assertEqual(message, "Save Rack.FCStd at 2026-09-28 09:05:07 for {owner}")

    def test_empty_or_broken_custom_message_falls_back_to_the_stamp(self) -> None:
        for template in ("", "   ", "{"):
            settings = {
                "auto_commit_message": "custom",
                "custom_commit_message": template,
            }
            self.assertEqual(
                build_commit_message(settings, ["Rack.FCStd"], moment=MOMENT),
                "Auto-save 2026-09-28 09:05:07 - Rack.FCStd",
            )

    def test_prefix_is_used_for_the_fallback_message(self) -> None:
        self.assertEqual(
            default_message("Exit save", ["Rack.FCStd"], moment=MOMENT),
            "Exit save 2026-09-28 09:05:07 - Rack.FCStd",
        )
        self.assertEqual(
            build_commit_message({}, ["Rack.FCStd"], prefix="Exit save", moment=MOMENT),
            "Exit save 2026-09-28 09:05:07 - Rack.FCStd",
        )

    def test_custom_message_applies_to_the_exit_prefix_too(self) -> None:
        settings = {
            "auto_commit_message": "custom",
            "custom_commit_message": "Session work {timestamp}",
        }
        self.assertEqual(
            build_commit_message(settings, ["Rack.FCStd"], prefix="Exit save", moment=MOMENT),
            "Session work 2026-09-28 09:05:07",
        )

    def test_message_is_never_empty(self) -> None:
        self.assertTrue(
            build_commit_message({}, [], prefix="", moment=MOMENT)
        )
        self.assertEqual(
            build_commit_message({}, [], prefix="", moment=MOMENT),
            "2026-09-28 09:05:07",
        )


if __name__ == "__main__":
    unittest.main()
