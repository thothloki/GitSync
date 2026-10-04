"""Tests for the rules governing which files a delete or rename touches."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gitsync.workspace_files import (
    WorkspaceFileError,
    preview_path,
    removal_paths,
    rename_pairs,
    rename_target,
)


class WorkspaceFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make(self, name: str, folder: str = "") -> Path:
        target = self.root / folder / name if folder else self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"model")
        return target

    def make_preview(self, model: Path) -> Path:
        preview = preview_path(model)
        preview.write_bytes(b"png")
        return preview


class PreviewPathTests(WorkspaceFileTestCase):
    def test_the_preview_sits_beside_the_model(self) -> None:
        self.assertEqual(
            preview_path(Path("/w/drawers/Divider.FCStd")),
            Path("/w/drawers/Divider.png"),
        )


class RemovalPathsTests(WorkspaceFileTestCase):
    def test_it_includes_the_model_and_its_preview(self) -> None:
        model = self.make("Rack.FCStd")
        preview = self.make_preview(model)
        self.assertEqual(removal_paths(model), [model, preview])

    def test_a_model_without_a_preview_is_just_the_model(self) -> None:
        model = self.make("Rack.FCStd")
        self.assertEqual(removal_paths(model), [model])

    def test_a_missing_model_is_refused(self) -> None:
        with self.assertRaises(WorkspaceFileError):
            removal_paths(self.root / "Gone.FCStd")

    def test_a_directory_is_refused(self) -> None:
        folder = self.root / "drawers"
        folder.mkdir()
        with self.assertRaises(WorkspaceFileError):
            removal_paths(folder)

    def test_a_symlinked_model_is_refused(self) -> None:
        real = self.make("Rack.FCStd")
        link = self.root / "Link.FCStd"
        link.symlink_to(real)
        with self.assertRaises(WorkspaceFileError) as caught:
            removal_paths(link)
        self.assertIn("symbolic link", str(caught.exception))
        # Nothing was touched: the link and its target both survive.
        self.assertTrue(link.is_symlink())
        self.assertTrue(real.is_file())

    def test_a_symlinked_preview_is_left_out(self) -> None:
        model = self.make("Rack.FCStd")
        other = self.make("Other.png")
        preview_path(model).symlink_to(other)
        # Only the model is listed, so Git never unlinks through the link.
        self.assertEqual(removal_paths(model), [model])
        self.assertTrue(other.is_file())

    def test_nothing_is_deleted_here(self) -> None:
        """This module only decides; Git performs the removal."""
        model = self.make("Rack.FCStd")
        preview = self.make_preview(model)
        removal_paths(model)
        self.assertTrue(model.is_file())
        self.assertTrue(preview.is_file())


class RenameTargetTests(WorkspaceFileTestCase):
    def test_it_returns_the_model_and_the_new_path(self) -> None:
        model = self.make("Rack.FCStd")
        source, target = rename_target(model, "Shelf.FCStd")
        self.assertEqual(source, model)
        self.assertEqual(target, self.root / "Shelf.FCStd")

    def test_a_suffix_is_appended_like_a_new_model(self) -> None:
        model = self.make("Rack.FCStd")
        _source, target = rename_target(model, "Shelf")
        self.assertEqual(target.name, "Shelf.FCStd")

    def test_the_model_stays_in_its_folder(self) -> None:
        model = self.make("Rack.FCStd", folder="drawers")
        _source, target = rename_target(model, "Shelf.FCStd")
        self.assertEqual(target, self.root / "drawers" / "Shelf.FCStd")

    def test_it_refuses_to_overwrite_an_existing_file(self) -> None:
        model = self.make("Rack.FCStd")
        self.make("Shelf.FCStd")
        with self.assertRaises(WorkspaceFileError):
            rename_target(model, "Shelf.FCStd")

    def test_it_refuses_a_case_only_change_on_any_file_system(self) -> None:
        # "rack.FCStd" must not clobber "Rack.FCStd" even on a case-sensitive
        # file system, because the repository may be checked out elsewhere.
        model = self.make("Rack.FCStd")
        with self.assertRaises(WorkspaceFileError):
            rename_target(model, "rack.FCStd")

    def test_the_same_name_is_refused(self) -> None:
        model = self.make("Rack.FCStd")
        with self.assertRaises(WorkspaceFileError):
            rename_target(model, "Rack.FCStd")

    def test_an_unsafe_name_is_refused_by_the_shared_validation(self) -> None:
        model = self.make("Rack.FCStd")
        for bad in ("../escape.FCStd", "sub/Other.FCStd", "Other:alt", "a*b"):
            with self.assertRaises(Exception):
                rename_target(model, bad)

    def test_a_dot_in_the_new_name_is_kept(self) -> None:
        model = self.make("Rack.FCStd")
        _source, target = rename_target(model, "Rev.3")
        self.assertEqual(target.name, "Rev.3.FCStd")
        # Typing the suffix as well must not double it.
        _source, target = rename_target(model, "Rev.3.FCStd")
        self.assertEqual(target.name, "Rev.3.FCStd")

    def test_a_missing_model_is_refused(self) -> None:
        with self.assertRaises(WorkspaceFileError):
            rename_target(self.root / "Gone.FCStd", "Shelf.FCStd")

    def test_a_symlink_is_refused(self) -> None:
        real = self.make("Rack.FCStd")
        link = self.root / "Link.FCStd"
        link.symlink_to(real)
        with self.assertRaises(WorkspaceFileError):
            rename_target(link, "Shelf.FCStd")
        self.assertTrue(real.is_file())

    def test_nothing_is_renamed_here(self) -> None:
        model = self.make("Rack.FCStd")
        rename_target(model, "Shelf.FCStd")
        self.assertTrue(model.is_file())
        self.assertFalse((self.root / "Shelf.FCStd").exists())


class RenamePairsTests(WorkspaceFileTestCase):
    def test_the_preview_follows_the_model(self) -> None:
        model = self.make("Rack.FCStd")
        preview = self.make_preview(model)
        target = self.root / "Shelf.FCStd"
        self.assertEqual(
            rename_pairs(model, target),
            [(model, target), (preview, self.root / "Shelf.png")],
        )

    def test_no_preview_means_a_single_pair(self) -> None:
        model = self.make("Rack.FCStd")
        self.assertEqual(
            rename_pairs(model, self.root / "Shelf.FCStd"),
            [(model, self.root / "Shelf.FCStd")],
        )

    def test_an_existing_target_preview_is_never_clobbered(self) -> None:
        model = self.make("Rack.FCStd")
        self.make_preview(model)
        keep = self.make("Shelf.png")
        pairs = rename_pairs(model, self.root / "Shelf.FCStd")
        self.assertEqual(len(pairs), 1)
        self.assertTrue(keep.is_file())

    def test_a_symlinked_preview_is_not_moved(self) -> None:
        model = self.make("Rack.FCStd")
        other = self.make("Other.png")
        preview_path(model).symlink_to(other)
        pairs = rename_pairs(model, self.root / "Shelf.FCStd")
        self.assertEqual(len(pairs), 1)
        self.assertTrue(other.is_file())


if __name__ == "__main__":
    unittest.main()