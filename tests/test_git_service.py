"""Tests for the non-GUI Git service."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from gitsync.git_service import (
    GitCommandError,
    GitError,
    GitRepository,
    _make_askpass_files,
    normalized_model_filename,
    validate_remote_url,
)


class GitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="gitsync-test-")
        self.root = Path(self.temp_dir.name) / "workspace"
        self.root.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "GitSync Test")
        self._git("config", "user.email", "gitsync@example.invalid")
        (self.root / "README.txt").write_text("one\n", encoding="utf-8")
        self._git("add", "README.txt")
        self._git("commit", "-m", "initial")
        self.repository = GitRepository(str(self.root))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            check=True,
            text=True,
            capture_output=True,
        )

    def test_askpass_does_not_treat_name_in_password_prompt_as_username(self) -> None:
        import os
        import tempfile

        if os.name == "nt":
            self.skipTest("POSIX askpass helper test")
        with tempfile.TemporaryDirectory() as directory:
            askpass = _make_askpass_files(Path(directory), "user", "token")
            environment = os.environ.copy()
            environment.update({"GITSYNC_USERNAME": "user", "GITSYNC_TOKEN": "token"})
            password = subprocess.run(
                [askpass, "Password for 'https://name-host/repo':"],
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            username = subprocess.run(
                [askpass, "Username for 'https://example.test':"],
                env=environment,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
        self.assertEqual(password, "token")
        self.assertEqual(username, "user")

    def test_git_errors_redact_tokens_and_url_userinfo(self) -> None:
        error = GitCommandError(
            ["git", "clone", "https://user:secret@example.test/repo.git"],
            128,
            "",
            "authentication failed for secret",
            "secret",
        )
        self.assertNotIn("secret", str(error))
        self.assertIn("***", str(error))

    def test_remote_url_validation(self) -> None:
        self.assertEqual(validate_remote_url(" https://example.test/a/repo.git "), "https://example.test/a/repo.git")
        self.assertEqual(validate_remote_url("http://example.test/repo.git"), "http://example.test/repo.git")
        with self.assertRaises(ValueError):
            validate_remote_url("ssh://example.test/repo.git")
        with self.assertRaises(ValueError):
            validate_remote_url("https://user:secret@example.test/repo.git")
        with self.assertRaises(ValueError):
            validate_remote_url("https://example.test/repo.git?token=secret")

    def test_snapshot_and_model_filter(self) -> None:
        (self.root / "part.step").write_text("STEP", encoding="utf-8")
        (self.root / "notes.txt").write_text("notes", encoding="utf-8")
        snapshot = self.repository.snapshot()
        self.assertEqual(snapshot["branch"], "main")
        self.assertIn("part.step", snapshot["models"])
        self.assertNotIn("notes.txt", snapshot["models"])

    def test_snapshot_reports_model_modification_times(self) -> None:
        (self.root / "Rack.FCStd").write_text("model", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "Bracket.step").write_text("solid", encoding="utf-8")
        snapshot = self.repository.snapshot()
        self.assertEqual(sorted(snapshot["models"]), ["Rack.FCStd", "sub/Bracket.step"])
        times = snapshot["model_mtimes"]
        self.assertEqual(sorted(times), ["Rack.FCStd", "sub/Bracket.step"])
        for relative, value in times.items():
            self.assertAlmostEqual(value, (self.root / relative).stat().st_mtime, places=3)
        # A model that disappears between listing and stat maps to 0.0.
        self.assertEqual(
            self.repository.model_mtimes(["missing.FCStd"]), {"missing.FCStd": 0.0}
        )

    def test_network_operations_reject_unapproved_remote(self) -> None:
        self._git("remote", "add", "origin", "ssh://example.test/repo.git")
        guarded = GitRepository(str(self.root), remote_url="https://example.test/repo.git")
        with self.assertRaises(GitError):
            guarded.fetch()

    def test_updating_remote_does_not_clear_credentials(self) -> None:
        self.repository.update_credentials(username="user", token="token")
        self.repository.update_credentials(remote_url="https://example.test/repo.git")
        self.assertEqual(self.repository.username, "user")
        self.assertEqual(self.repository.token, "token")

    def test_commit_paths_does_not_stage_unselected_file(self) -> None:
        (self.root / "README.txt").write_text("changed\n", encoding="utf-8")
        (self.root / "other.txt").write_text("other\n", encoding="utf-8")
        result = self.repository.commit_paths(["README.txt"], "only readme")
        self.assertTrue(result["created"])
        status = self.repository.status()
        paths = [entry["path"] for entry in status["entries"]]
        self.assertIn("other.txt", paths)
        self.assertNotIn("README.txt", paths)

    def test_nested_workspace_is_not_treated_as_parent_repository(self) -> None:
        nested = self.root / "nested"
        nested.mkdir()
        nested_repo = GitRepository(str(nested))
        self.assertFalse(nested_repo.is_repository())
        with self.assertRaises(GitError):
            nested_repo.require_repository()

    def test_model_list_rejects_symlink_escape(self) -> None:
        outside = Path(self.temp_dir.name) / "outside.FCStd"
        outside.write_text("not in repo", encoding="utf-8")
        inside = self.root / "inside.FCStd"
        inside.write_text("inside", encoding="utf-8")
        for name, target in (("linked.FCStd", outside), ("inside-link.FCStd", inside)):
            link = self.root / name
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available")
        models = self.repository.model_files()
        self.assertNotIn("linked.FCStd", models)
        self.assertNotIn("inside-link.FCStd", models)
        with self.assertRaises(GitError):
            self.repository.safe_model_path("linked.FCStd")
        with self.assertRaises(GitError):
            self.repository.safe_model_path("inside-link.FCStd")

    def test_non_origin_upstream_is_used_for_fetch(self) -> None:
        remote = Path(self.temp_dir.name) / "upstream.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self._git("remote", "add", "upstream", str(remote))
        self._git("push", "-u", "upstream", "main")
        repository = GitRepository(str(self.root), remote_url=str(remote))
        self.assertEqual(repository.upstream_remote(), "upstream")
        repository.fetch()
        self.assertEqual(repository.upstream(), "upstream/main")

    def test_force_add_can_track_generated_png(self) -> None:
        (self.root / ".gitignore").write_text("*.png\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore png")
        (self.root / "preview.png").write_bytes(b"png")
        result = self.repository.commit_paths(
            ["preview.png"],
            "generated preview",
            force_add=True,
        )
        self.assertTrue(result["created"])
        self.assertTrue(self.repository.status()["clean"])

    def test_commit_force_adds_preview_png_but_respects_other_ignore_rules(self) -> None:
        # A repository may ignore *.png; previews are force-added in their own
        # invocation while ignore rules for the model files stay in effect.
        (self.root / ".gitignore").write_text("*.png\nignored.FCStd\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore rules")
        (self.root / "Rack.FCStd").write_text("model", encoding="utf-8")
        (self.root / "Rack.png").write_bytes(b"png")
        (self.root / "ignored.FCStd").write_text("model", encoding="utf-8")
        # An explicitly selected ignored model is reported instead of being
        # silently added with -f.
        with self.assertRaises(GitCommandError):
            self.repository.commit_paths(
                ["Rack.FCStd", "Rack.png", "ignored.FCStd"],
                "model with preview",
            )
        result = self.repository.commit_paths(
            ["Rack.FCStd", "Rack.png"],
            "model with preview",
        )
        self.assertTrue(result["created"])
        committed = self._git("show", "--name-only", "--pretty=format:").stdout.split()
        self.assertEqual(sorted(committed), ["Rack.FCStd", "Rack.png"])
        tracked = self._git("ls-files").stdout.split()
        self.assertNotIn("ignored.FCStd", tracked)
        self.assertTrue(self.repository.status()["clean"])

    def test_force_add_still_overrides_ignore_rules(self) -> None:
        (self.root / ".gitignore").write_text("*.FCStd\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore models")
        (self.root / "forced.FCStd").write_text("model", encoding="utf-8")
        result = self.repository.commit_paths(["forced.FCStd"], "forced", force_add=True)
        self.assertTrue(result["created"])
        self.assertTrue(self.repository.status()["clean"])

    def test_selected_path_is_literal(self) -> None:
        selected = self.root / "selected[1].FCStd"
        other = self.root / "other.FCStd"
        selected.write_text("selected", encoding="utf-8")
        other.write_text("other", encoding="utf-8")
        result = self.repository.commit_paths([selected.name], "literal path")
        self.assertTrue(result["created"])
        status_paths = [entry["path"] for entry in self.repository.status()["entries"]]
        self.assertIn(other.name, status_paths)
        self.assertNotIn(selected.name, status_paths)

    def test_selected_rename_commits_both_paths(self) -> None:
        (self.root / "README.txt").rename(self.root / "README-renamed.txt")
        self._git("add", "-A")
        entry = self.repository.status()["entries"][0]
        result = self.repository.commit_paths(entry["related_paths"], "rename file")
        self.assertTrue(result["created"])
        self.assertTrue(self.repository.status()["clean"])

    def test_selected_commit_leaves_preexisting_staged_file_alone(self) -> None:
        (self.root / "other.txt").write_text("other\n", encoding="utf-8")
        self._git("add", "other.txt")
        (self.root / "README.txt").write_text("selected\n", encoding="utf-8")
        result = self.repository.commit_paths(["README.txt"], "selected only")
        self.assertTrue(result["created"])
        staged = self.repository.run(["diff", "--cached", "--name-only"], check=False).output
        self.assertIn("other.txt", staged.splitlines())
        self.assertNotIn("README.txt", staged.splitlines())

    def test_new_model_file_name_validation(self) -> None:
        self.assertEqual(normalized_model_filename("Rack"), "Rack.FCStd")
        self.assertEqual(normalized_model_filename("Rack.FCStd"), "Rack.FCStd")
        self.assertEqual(normalized_model_filename("  my part  "), "my part.FCStd")
        self.assertEqual(
            normalized_model_filename("bracketed[1].fcstd"),
            "bracketed[1].FCStd",
        )
        for value in (
            "",
            "   ",
            ".",
            "..",
            ".hidden",
            ".FCStd",
            "sub/part",
            "sub\\part",
            "../escape",
            "part:alt",
            "trailing.",
            "CON.test",
            "NUL",
            "COM1",
            "a" * 130,
        ):
            with self.assertRaises(GitError, msg=value):
                normalized_model_filename(value)

    def test_a_dot_in_the_name_is_not_read_as_an_extension(self) -> None:
        # "0.test" and "0-test" must behave the same: a dot is part of the
        # name, and only an explicit .FCStd ending is honoured.
        for value, expected in (
            ("0-test", "0-test.FCStd"),
            ("0.test", "0.test.FCStd"),
            ("v2.0", "v2.0.FCStd"),
            ("Rev.3 bracket", "Rev.3 bracket.FCStd"),
            ("part.step", "part.step.FCStd"),
            # An explicit suffix is never doubled, in any casing.
            ("Rack.FCStd", "Rack.FCStd"),
            ("Rack.fcstd", "Rack.FCStd"),
            ("0.test.FCStd", "0.test.FCStd"),
            ("0.test.fcstd", "0.test.FCStd"),
        ):
            self.assertEqual(normalized_model_filename(value), expected, value)

    def test_new_model_name_allows_git_pathspec_characters(self) -> None:
        # Brackets are valid in file names and are only special to Git
        # pathspecs, which GitSync always passes literally.
        self.assertEqual(normalized_model_filename("rack[1]"), "rack[1].FCStd")
        # An asterisk is refused because other platforms cannot store it.
        with self.assertRaises(GitError):
            normalized_model_filename("rack*")

    def test_commit_all_ignores_previews_that_do_not_exist(self) -> None:
        # A model without a rendered preview must not break a working-tree
        # commit: "git add" rejects a pathspec that matches nothing.
        (self.root / "Rack.FCStd").write_text("model", encoding="utf-8")
        (self.root / "Other.step").write_text("solid", encoding="utf-8")
        result = self.repository.commit_all("working tree")
        self.assertTrue(result["created"])
        committed = self._git("show", "--name-only", "--pretty=format:").stdout.split()
        self.assertEqual(sorted(committed), ["Other.step", "Rack.FCStd"])

    def test_commit_all_stages_a_deleted_preview_removal(self) -> None:
        (self.root / ".gitignore").write_text("*.png\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore png")
        (self.root / "Rack.FCStd").write_text("model", encoding="utf-8")
        (self.root / "Rack.png").write_bytes(b"png")
        self.repository.commit_all("add model")
        self.assertIn("Rack.png", self._git("ls-files").stdout.split())
        # Deleting the model and its preview must commit both removals.
        (self.root / "Rack.FCStd").unlink()
        (self.root / "Rack.png").unlink()
        result = self.repository.commit_all("remove model")
        self.assertTrue(result["created"])
        self.assertTrue(self.repository.status()["clean"])
        self.assertNotIn("Rack.png", self._git("ls-files").stdout.split())

    def test_commit_all_force_adds_an_ignored_preview(self) -> None:
        (self.root / ".gitignore").write_text("*.png\n", encoding="utf-8")
        self._git("add", ".gitignore")
        self._git("commit", "-m", "ignore png")
        (self.root / "Rack.FCStd").write_text("model", encoding="utf-8")
        (self.root / "Rack.png").write_bytes(b"png")
        self.repository.commit_all("model with preview")
        self.assertIn("Rack.png", self._git("ls-files").stdout.split())

    def test_push_and_pull_with_local_bare_remote(self) -> None:
        remote = Path(self.temp_dir.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        self._git("remote", "add", "origin", str(remote))
        self._git("push", "-u", "origin", "main")
        self.repository.update_credentials(remote_url=str(remote))
        self.repository.push()

        clone = Path(self.temp_dir.name) / "clone"
        subprocess.run(["git", "clone", "-b", "main", str(remote), str(clone)], check=True, capture_output=True)
        clone_repo = GitRepository(str(clone), remote_url=str(remote))
        (self.root / "README.txt").write_text("remote update\n", encoding="utf-8")
        self._git("add", "README.txt")
        self._git("commit", "-m", "remote update")
        self._git("push")
        clone_repo.pull()
        self.assertEqual((clone / "README.txt").read_text(encoding="utf-8"), "remote update\n")


class GitRemoveAndRenameTests(GitServiceTests):
    """``git rm`` and ``git mv``, so a delete or rename lands in a commit."""

    def _commit_model(self, name: str, body: str = "model\n") -> Path:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        preview = target.with_suffix(".png")
        preview.write_text("png\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "add {}".format(name))
        return target

    def _messages(self) -> list:
        result = subprocess.run(
            ["git", "log", "--format=%s"],
            cwd=str(self.root), check=True, text=True, capture_output=True,
        )
        return [line for line in result.stdout.splitlines() if line]

    def _status(self) -> str:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.root), check=True, text=True, capture_output=True,
        )
        return result.stdout.strip()

    # ------------------------------------------------------------------
    # remove_paths
    # ------------------------------------------------------------------
    def test_remove_paths_commits_the_deletion(self) -> None:
        model = self._commit_model("Rack.FCStd")
        result = self.repository.remove_paths(["Rack.FCStd"], "2026-10-03 21:00:00 deleted")
        self.assertTrue(result["created"])
        self.assertEqual(result["removed"], ["Rack.FCStd"])
        self.assertFalse(model.exists())
        # Committed, so the working tree is clean.
        self.assertEqual(self._status(), "")
        self.assertEqual(self._messages()[0], "2026-10-03 21:00:00 deleted")
        listed = subprocess.run(
            ["git", "ls-files", "--", "Rack.FCStd"],
            cwd=str(self.root), check=True, text=True, capture_output=True,
        )
        self.assertEqual(listed.stdout.strip(), "")

    def test_remove_paths_takes_the_preview_with_it(self) -> None:
        self._commit_model("Rack.FCStd")
        result = self.repository.remove_paths(
            ["Rack.FCStd", "Rack.png"], "2026-10-03 21:00:00 deleted"
        )
        self.assertEqual(sorted(result["removed"]), ["Rack.FCStd", "Rack.png"])
        self.assertFalse((self.root / "Rack.png").exists())
        self.assertEqual(self._status(), "")

    def test_remove_paths_refuses_a_file_with_uncommitted_edits(self) -> None:
        model = self._commit_model("Rack.FCStd")
        model.write_text("model\nlocal edit\n", encoding="utf-8")
        with self.assertRaises(GitCommandError) as caught:
            self.repository.remove_paths(["Rack.FCStd"], "deleted")
        # This is the guard against discarding work that was never committed.
        self.assertIn("local modifications", str(caught.exception))
        self.assertTrue(model.exists())
        self.assertNotIn("deleted", self._messages())

    def test_remove_paths_drops_an_untracked_file_without_a_commit(self) -> None:
        stray = self.root / "Stray.FCStd"
        stray.write_text("new\n", encoding="utf-8")
        result = self.repository.remove_paths(["Stray.FCStd"], "deleted")
        self.assertFalse(stray.exists())
        self.assertEqual(result["unlinked"], ["Stray.FCStd"])
        self.assertEqual(result["removed"], [])
        # Nothing was tracked, so there is nothing to commit.
        self.assertFalse(result["created"])
        self.assertEqual(self._messages()[0], "initial")

    def test_remove_paths_leaves_unrelated_staged_changes_out(self) -> None:
        self._commit_model("Rack.FCStd")
        other = self.root / "Other.txt"
        other.write_text("mine\n", encoding="utf-8")
        self._git("add", "Other.txt")          # staged, but not ours
        self.repository.remove_paths(["Rack.FCStd"], "2026-10-03 21:00:00 deleted")
        self.assertEqual(self._messages()[0], "2026-10-03 21:00:00 deleted")
        # The user's own staged change is still staged, not swept into the commit.
        self.assertEqual(self._status(), "A  Other.txt")

    def test_remove_paths_needs_a_message(self) -> None:
        self._commit_model("Rack.FCStd")
        with self.assertRaises(GitError):
            self.repository.remove_paths(["Rack.FCStd"], "   ")

    def test_remove_paths_rejects_a_path_outside_the_workspace(self) -> None:
        with self.assertRaises(GitError):
            self.repository.remove_paths(["../escape.FCStd"], "deleted")

    # ------------------------------------------------------------------
    # move_paths
    # ------------------------------------------------------------------
    def test_move_paths_commits_a_rename(self) -> None:
        model = self._commit_model("Rack.FCStd")
        result = self.repository.move_paths(
            [("Rack.FCStd", "Shelf.FCStd")], "2026-10-03 21:00:00 renamed"
        )
        self.assertTrue(result["created"])
        self.assertFalse(model.exists())
        self.assertTrue((self.root / "Shelf.FCStd").is_file())
        self.assertEqual(self._status(), "")
        self.assertEqual(self._messages()[0], "2026-10-03 21:00:00 renamed")
        shown = subprocess.run(
            ["git", "show", "--name-status", "--format=", "HEAD"],
            cwd=str(self.root), check=True, text=True, capture_output=True,
        ).stdout
        # Git records it as a rename, not a delete plus an add.
        self.assertIn("R100", shown)

    def test_move_paths_moves_the_preview_in_the_same_commit(self) -> None:
        self._commit_model("Rack.FCStd")
        self.repository.move_paths(
            [("Rack.FCStd", "Shelf.FCStd"), ("Rack.png", "Shelf.png")],
            "2026-10-03 21:00:00 renamed",
        )
        self.assertTrue((self.root / "Shelf.png").is_file())
        self.assertFalse((self.root / "Rack.png").exists())
        self.assertEqual(self._status(), "")
        # One commit, not one per file.
        self.assertEqual(
            self._messages()[:2], ["2026-10-03 21:00:00 renamed", "add Rack.FCStd"]
        )

    def test_move_paths_tracks_a_previously_untracked_model(self) -> None:
        stray = self.root / "Stray.FCStd"
        stray.write_text("new\n", encoding="utf-8")
        result = self.repository.move_paths(
            [("Stray.FCStd", "Moved.FCStd")], "2026-10-03 21:00:00 renamed"
        )
        self.assertTrue(result["created"])
        self.assertTrue((self.root / "Moved.FCStd").is_file())
        self.assertEqual(self._status(), "")

    def test_move_paths_needs_a_message(self) -> None:
        self._commit_model("Rack.FCStd")
        with self.assertRaises(GitError):
            self.repository.move_paths([("Rack.FCStd", "Shelf.FCStd")], "")

    def test_move_paths_rejects_a_target_outside_the_workspace(self) -> None:
        self._commit_model("Rack.FCStd")
        with self.assertRaises(GitError):
            self.repository.move_paths([("Rack.FCStd", "../escape.FCStd")], "renamed")


class StalePreviewTests(GitServiceTests):
    """A model deleted outside GitSync must not leave its preview behind."""

    def _add_model(self, name: str, png: bytes = b"png\n") -> None:
        (self.root / name).write_text("model\n", encoding="utf-8")
        (self.root / name.replace(".FCStd", ".png")).write_bytes(png)
        self._git("add", "-A")
        self._git("commit", "-m", "add {}".format(name))

    def test_a_deleted_model_loses_its_preview_in_the_working_tree_commit(self) -> None:
        self._add_model("Rack.FCStd")
        self._add_model("Keep.FCStd")
        # Deleted behind GitSync's back: still tracked, missing from disk.
        (self.root / "Rack.FCStd").unlink()
        self.assertTrue((self.root / "Rack.png").is_file())

        result = self.repository.commit_all("2026-10-03 12:00:00 deleted")

        self.assertTrue(result["created"])
        self.assertEqual(result["dropped_previews"], ["Rack.png"])
        self.assertFalse((self.root / "Rack.png").exists())
        shown = subprocess.run(
            ["git", "show", "--name-status", "--format=", "HEAD"],
            cwd=str(self.root), check=True, text=True, capture_output=True,
        ).stdout
        # Both the model and its preview are recorded, so nothing is orphaned.
        self.assertIn("D\tRack.FCStd", shown)
        self.assertIn("D\tRack.png", shown)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(self.root),
            check=True, text=True, capture_output=True,
        ).stdout.strip()
        self.assertEqual(status, "")

    def test_a_live_models_preview_is_never_touched(self) -> None:
        self._add_model("Keep.FCStd")
        self.repository.commit_all("nothing to do")
        self.assertTrue((self.root / "Keep.png").is_file())
        listed = subprocess.run(
            ["git", "ls-files", "--", "Keep.png"], cwd=str(self.root),
            check=True, text=True, capture_output=True,
        ).stdout
        self.assertEqual(listed.strip(), "Keep.png")

    def test_a_regenerated_preview_is_still_removed(self) -> None:
        # A preview changed since the last commit differs from the index, so
        # `git rm` would refuse it.  A plain unlink stages the removal anyway.
        self._add_model("Rack.FCStd")
        (self.root / "Rack.png").write_bytes(b"a completely different picture\n")
        (self.root / "Rack.FCStd").unlink()
        result = self.repository.commit_all("2026-10-03 12:00:00 deleted")
        self.assertEqual(result["dropped_previews"], ["Rack.png"])
        self.assertFalse((self.root / "Rack.png").exists())

    def test_a_model_deleted_by_gitsync_needs_no_second_removal(self) -> None:
        # remove_paths already deleted the preview; commit_all must not try
        # again or report a preview it did not drop.
        self._add_model("Rack.FCStd")
        self.repository.remove_paths(
            ["Rack.FCStd", "Rack.png"], "2026-10-03 12:00:00 deleted"
        )
        result = self.repository.commit_all("2026-10-03 12:01:00 tidy up")
        self.assertEqual(result["dropped_previews"], [])
        self.assertFalse(result["created"])

    def test_a_deleted_non_model_file_leaves_no_trace(self) -> None:
        (self.root / "notes.txt").write_text("mine\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "notes")
        (self.root / "notes.txt").unlink()
        result = self.repository.commit_all("2026-10-03 12:00:00 deleted")
        self.assertEqual(result["dropped_previews"], [])

    def test_a_folder_named_like_a_preview_is_not_removed(self) -> None:
        self._add_model("Rack.FCStd")
        (self.root / "Rack.FCStd").unlink()
        # A *directory* named Rack.png is not a preview.
        (self.root / "Rack.png").unlink()
        (self.root / "Rack.png").mkdir()
        try:
            result = self.repository.commit_all("2026-10-03 12:00:00 deleted")
            self.assertEqual(result["dropped_previews"], [])
            self.assertTrue((self.root / "Rack.png").is_dir())
        finally:
            (self.root / "Rack.png").rmdir()


if __name__ == "__main__":
    unittest.main()
