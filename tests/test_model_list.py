"""Tests for the model list grouping and ordering."""

from __future__ import annotations

import unittest
from datetime import datetime

from gitsync.model_list import (
    MODIFIED_SORT,
    NAME_SORT,
    filter_models,
    format_timestamp,
    group_models,
    matches_filter,
    newest_time,
    normalize_sort_order,
    sorted_models,
)


class ModelListTests(unittest.TestCase):
    def test_name_order_is_case_insensitive(self) -> None:
        models = ["beta.FCStd", "Alpha.step", "gamma.FCStd"]
        self.assertEqual(
            sorted_models(models, sort_order=NAME_SORT),
            ["Alpha.step", "beta.FCStd", "gamma.FCStd"],
        )

    def test_modified_order_moves_folders_by_their_newest_file(self) -> None:
        models = ["root.FCStd", "sub/newest.step", "sub/older.step", "zebra/late.FCStd"]
        times = {
            "root.FCStd": 500.0,
            "sub/newest.step": 9000.0,
            "sub/older.step": 100.0,
            "zebra/late.FCStd": 700.0,
        }
        grouped = group_models(models, times, sort_order=MODIFIED_SORT)
        self.assertEqual(
            grouped,
            [
                ("sub", ["sub/newest.step", "sub/older.step"]),
                ("zebra", ["zebra/late.FCStd"]),
                ("", ["root.FCStd"]),
            ],
        )

    def test_unknown_order_falls_back_to_name(self) -> None:
        self.assertEqual(normalize_sort_order("nonsense"), NAME_SORT)
        self.assertEqual(normalize_sort_order(""), NAME_SORT)
        self.assertEqual(normalize_sort_order(None), NAME_SORT)
        self.assertEqual(normalize_sort_order("MODIFIED"), MODIFIED_SORT)

    def test_folders_are_alphabetical_with_the_models(self) -> None:
        models = [
            "zebra/one.FCStd",
            "apple/two.FCStd",
            "root.FCStd",
            "alpha/three.FCStd",
        ]
        grouped = group_models(models, sort_order=NAME_SORT)
        # A folder is ordered among the root models, not in a block of its own,
        # so "root.FCStd" lands between "apple" and "zebra".
        self.assertEqual(
            grouped,
            [
                ("alpha", ["alpha/three.FCStd"]),
                ("apple", ["apple/two.FCStd"]),
                ("", ["root.FCStd"]),
                ("zebra", ["zebra/one.FCStd"]),
            ],
        )

    def test_folder_name_order_ignores_modification_times(self) -> None:
        models = ["b.FCStd", "sub/a.FCStd", "a.FCStd"]
        times = {"b.FCStd": 9999.0, "sub/a.FCStd": 1.0, "a.FCStd": 2.0}
        grouped = group_models(models, times, sort_order=NAME_SORT)
        # Even though "sub" holds the oldest file and "b.FCStd" the newest,
        # the name order only looks at the labels.
        self.assertEqual(
            grouped,
            [("", ["a.FCStd"]), ("", ["b.FCStd"]), ("sub", ["sub/a.FCStd"])],
        )

    def test_modified_order_places_a_folder_by_its_newest_file(self) -> None:
        models = [
            "middle.FCStd",
            "stale/old.FCStd",
            "fresh/new.FCStd",
            "fresh/old.FCStd",
            "last.FCStd",
        ]
        times = {
            "middle.FCStd": 500.0,
            "stale/old.FCStd": 100.0,
            "fresh/new.FCStd": 9000.0,
            "fresh/old.FCStd": 200.0,
            "last.FCStd": 100.0,
        }
        grouped = group_models(models, times, sort_order=MODIFIED_SORT)
        # The "fresh" folder leads because its newest file is the newest thing
        # in the repository, and it interleaves with the root models.
        self.assertEqual(
            grouped,
            [
                ("fresh", ["fresh/new.FCStd", "fresh/old.FCStd"]),
                ("", ["middle.FCStd"]),
                ("", ["last.FCStd"]),
                ("stale", ["stale/old.FCStd"]),
            ],
        )

    def test_modified_order_falls_back_to_the_name_for_a_tie(self) -> None:
        models = ["zzz.FCStd", "aaa.FCStd", "mmm/one.FCStd"]
        times = {"zzz.FCStd": 700.0, "aaa.FCStd": 700.0, "mmm/one.FCStd": 700.0}
        grouped = group_models(models, times, sort_order=MODIFIED_SORT)
        self.assertEqual(
            grouped,
            [
                ("", ["aaa.FCStd"]),
                ("mmm", ["mmm/one.FCStd"]),
                ("", ["zzz.FCStd"]),
            ],
        )

    def test_a_folder_without_times_sorts_last(self) -> None:
        models = ["unknown/one.FCStd", "dated.FCStd"]
        times = {"dated.FCStd": 500.0}
        grouped = group_models(models, times, sort_order=MODIFIED_SORT)
        self.assertEqual(
            grouped,
            [("", ["dated.FCStd"]), ("unknown", ["unknown/one.FCStd"])],
        )

    def test_nested_folders_use_a_posix_prefix(self) -> None:
        grouped = group_models(["a/b/deep.FCStd", "a/one.FCStd"], sort_order=NAME_SORT)
        # Files keep their repository-relative path; only the grouping key is
        # the folder.
        self.assertEqual(
            grouped, [("a", ["a/one.FCStd"]), ("a/b", ["a/b/deep.FCStd"])]
        )

    def test_modified_order_is_newest_first(self) -> None:
        models = ["old.FCStd", "new.FCStd", "middle.FCStd"]
        times = {"old.FCStd": 1000.0, "new.FCStd": 3000.0, "middle.FCStd": 2000.0}
        self.assertEqual(
            sorted_models(models, times, sort_order=MODIFIED_SORT),
            ["new.FCStd", "middle.FCStd", "old.FCStd"],
        )

    def test_modified_order_sorts_each_folder_separately(self) -> None:
        models = ["a/one.FCStd", "a/two.FCStd", "b/three.FCStd"]
        times = {
            "a/one.FCStd": 100.0,
            "a/two.FCStd": 900.0,
            "b/three.FCStd": 500.0,
        }
        grouped = group_models(models, times, sort_order=MODIFIED_SORT)
        self.assertEqual(
            grouped,
            [("a", ["a/two.FCStd", "a/one.FCStd"]), ("b", ["b/three.FCStd"])],
        )

    def test_models_without_a_time_come_last(self) -> None:
        models = ["known.FCStd", "unknown.FCStd"]
        times = {"known.FCStd": 500.0}
        self.assertEqual(
            sorted_models(models, times, sort_order=MODIFIED_SORT),
            ["known.FCStd", "unknown.FCStd"],
        )

    def test_equal_times_fall_back_to_the_name(self) -> None:
        models = ["b.FCStd", "a.FCStd"]
        times = {"a.FCStd": 700.0, "b.FCStd": 700.0}
        self.assertEqual(
            sorted_models(models, times, sort_order=MODIFIED_SORT),
            ["a.FCStd", "b.FCStd"],
        )

    def test_empty_and_missing_inputs(self) -> None:
        self.assertEqual(group_models([], sort_order=NAME_SORT), [])
        self.assertEqual(group_models(["", None], sort_order=NAME_SORT), [])
        # A missing time map must not raise in the modified order.
        self.assertEqual(
            sorted_models(["a.FCStd"], None, sort_order=MODIFIED_SORT),
            ["a.FCStd"],
        )

    def test_format_timestamp(self) -> None:
        moment = datetime(2026, 9, 29, 14, 5, 9).timestamp()
        # Date only: the time is dropped so the column can stay narrow.
        self.assertEqual(format_timestamp(moment), "2026-09-29")
        self.assertEqual(format_timestamp(None), "")
        self.assertEqual(format_timestamp(0.0), "")

    def test_format_timestamp_ignores_the_time_of_day(self) -> None:
        morning = datetime(2026, 9, 29, 1, 0, 0).timestamp()
        evening = datetime(2026, 9, 29, 23, 59, 59).timestamp()
        self.assertEqual(format_timestamp(morning), format_timestamp(evening))

    def test_newest_time_takes_the_most_recent_file(self) -> None:
        files = ["a.FCStd", "b.FCStd", "c.FCStd"]
        times = {"a.FCStd": 100.0, "b.FCStd": 900.0}
        self.assertEqual(newest_time(files, times), 900.0)
        # An unknown time counts as the epoch rather than raising.
        self.assertEqual(newest_time(files, None), 0.0)
        self.assertEqual(newest_time([], {"a.FCStd": 5.0}), 0.0)

    # ------------------------------------------------------------------
    # search box
    # ------------------------------------------------------------------
    def test_empty_filter_keeps_every_model(self) -> None:
        models = ["A.FCStd", "sub/B.step"]
        for term in ("", "   ", None):
            self.assertEqual(filter_models(models, term), models)

    def test_filter_matches_file_names_case_insensitively(self) -> None:
        models = ["StorageBin_Rack.FCStd", "thread.FCStd", "sub/Bracket.step"]
        self.assertEqual(filter_models(models, "storagebin"), ["StorageBin_Rack.FCStd"])
        self.assertEqual(filter_models(models, "THREAD"), ["thread.FCStd"])
        self.assertEqual(filter_models(models, "STEP"), ["sub/Bracket.step"])
        self.assertEqual(filter_models(models, "nothing"), [])
        # The term is a plain substring of the path, so it can also match part
        # of a file name: "rack" matches "Bracket" as well.
        self.assertEqual(
            filter_models(models, "rack"),
            ["StorageBin_Rack.FCStd", "sub/Bracket.step"],
        )

    def test_filter_also_matches_the_folder(self) -> None:
        models = ["sub/one.FCStd", "sub/two.FCStd", "root.FCStd"]
        self.assertEqual(filter_models(models, "sub/"), models[:2])

    def test_filter_trims_the_term(self) -> None:
        self.assertEqual(filter_models(["A.FCStd"], "  a  "), ["A.FCStd"])

    def test_matches_filter_helper(self) -> None:
        self.assertTrue(matches_filter("a/b.FCStd", ""))
        self.assertTrue(matches_filter("a/b.FCStd", "B.FC"))
        self.assertTrue(matches_filter("a/b.FCStd", "FCSTD"))
        self.assertFalse(matches_filter("a/b.FCStd", "zzz"))


if __name__ == "__main__":
    unittest.main()
