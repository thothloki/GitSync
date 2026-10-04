"""Tests for the startup comparison dialog actions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qt_stub  # noqa: E402

qt_stub.install()

from gitsync.dialogs import StartupSyncDialog  # noqa: E402


def _actions(summary):
    """Return the (label, choice) pairs the dialog would show for a summary."""
    dialog = StartupSyncDialog.__new__(StartupSyncDialog)
    dialog.summary = summary
    recorded = []
    dialog._add_button = lambda text, choice: recorded.append((text, choice))
    dialog._add_actions()
    return recorded


class StartupDialogTests(unittest.TestCase):
    def test_uncommitted_changes_offer_one_continue_button(self) -> None:
        actions = _actions({"entries": [{"path": "Rack.FCStd"}], "upstream": "origin/main"})
        self.assertEqual(
            actions,
            [
                ("Commit the uncommitted changes", "commit"),
                ("Continue without changing files", "close"),
            ],
        )
        labels = [label for label, _ in actions]
        self.assertEqual(len(labels), len(set(labels)), "duplicate action labels")

    def test_clean_tree_only_offers_continue(self) -> None:
        self.assertEqual(
            _actions({"upstream": "origin/main"}),
            [("Continue without changing files", "close")],
        )

    def test_ahead_offers_push(self) -> None:
        self.assertEqual(
            _actions({"upstream": "origin/main", "ahead": 2}),
            [("Push local commits", "push"), ("Continue without changing files", "close")],
        )

    def test_behind_offers_pull(self) -> None:
        self.assertEqual(
            _actions({"upstream": "origin/main", "behind": 2}),
            [("Pull remote changes", "pull"), ("Continue without changing files", "close")],
        )

    def test_diverged_offers_merge_and_rebase(self) -> None:
        self.assertEqual(
            _actions({"upstream": "origin/main", "ahead": 1, "behind": 3}),
            [
                ("Merge remote changes", "merge"),
                ("Rebase local commits", "rebase"),
                ("Continue without changing files", "close"),
            ],
        )

    def test_remote_without_upstream_offers_set_upstream(self) -> None:
        self.assertEqual(
            _actions({"remote": "origin", "upstream": "", "branch": "main"}),
            [
                ("Set upstream to origin/main", "upstream"),
                ("Continue without changing files", "close"),
            ],
        )

    def test_uncommitted_rows_list_every_change(self) -> None:
        entries = [{"status": " M", "path": "A.FCStd"}, {"status": "??", "path": "b.step"}]
        self.assertEqual(
            StartupSyncDialog._changes_rows(entries),
            [" M  A.FCStd", "??  b.step"],
        )
        self.assertEqual(StartupSyncDialog._changes_rows([]), [])

    def test_long_change_lists_get_a_summary_row(self) -> None:
        limit = StartupSyncDialog._CHANGES_LIMIT
        entries = [{"status": " M", "path": "p{}.FCStd".format(i)} for i in range(limit + 50)]
        rows = StartupSyncDialog._changes_rows(entries)
        self.assertEqual(len(rows), limit + 1)
        self.assertEqual(rows[-1], "... and 50 more change(s)")


if __name__ == "__main__":
    unittest.main()
