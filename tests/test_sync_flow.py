"""Tests for commit/push coordination: previews belong to commits, not pushes."""

from __future__ import annotations

import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qt_stub  # noqa: E402

qt_stub.install()

from gitsync import sync_panel  # noqa: E402
from gitsync.commit_message import build_commit_message  # noqa: E402
from gitsync.configuration import SettingsStore, _MemoryParameters  # noqa: E402
from gitsync.git_service import GitError, GitRepository  # noqa: E402
from gitsync.model_list import MODIFIED_SORT, NAME_SORT, format_timestamp  # noqa: E402
from gitsync.model_preview import is_supported_model  # noqa: E402
from gitsync.qt_compat import palette_is_dark, relative_luminance  # noqa: E402
from gitsync.sync_events import SyncController  # noqa: E402
from gitsync.sync_panel import SyncPanel  # noqa: E402


class _ImmediateRunner:
    """Minimal stand-in for TaskRunner that runs work on the calling thread."""

    def __init__(self) -> None:
        self.busy = False
        self.submitted: List[Any] = []
        self.errors: List[BaseException] = []

    def submit(self, function, *args, on_success=None, on_error=None, **kwargs) -> None:
        self.submitted.append(function)
        try:
            result = function(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - surfaced by the test
            self.errors.append(exc)
            if on_error is not None:
                on_error(exc)
            return
        if on_success is not None:
            on_success(result)

    def wait_for_idle(self, timeout_ms: int = 5000) -> bool:
        return True

    def shutdown(self, timeout_ms: int = 5000) -> None:
        return None


class _FakeContext:
    def __init__(self, repository: GitRepository, settings: Dict[str, Any]) -> None:
        self.repository = repository
        self.settings = settings
        self.settings_store = SettingsStore(_MemoryParameters())
        self.generation = 0
        self.runner = _ImmediateRunner()
        self.panel = None


class _Repository:
    """Repository stand-in recording the Git calls the panel makes.

    The real ``GitRepository`` is not used here: these tests are about what the
    panel *asks* of Git, so the calls are captured instead of executed.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.removed: List[Any] = []
        self.moved: List[Any] = []
        self.pushed = 0
        self.push_ok = True

    def relative_path(self, path: str) -> str:
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def remove_paths(self, paths, message):
        self.removed.append((list(paths), message))
        return {"created": True, "removed": list(paths), "unlinked": [],
                "message": message}

    def move_paths(self, moves, message):
        self.moved.append((list(moves), message))
        return {"created": True, "moved": [list(pair) for pair in moves],
                "message": message}

    def push(self, *args, **kwargs):
        self.pushed += 1
        return type("_Pushed", (), {"ok": self.push_ok})()


class _FakeDocument:
    def __init__(self, name: str, file_name: str, modified: bool = False) -> None:
        self.Name = name
        self.FileName = file_name
        self.Modified = modified

    def isTouched(self) -> bool:
        return bool(self.Modified)


class _RecordingPreview:
    """ModelPreview stand-in that writes a placeholder PNG for a model."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.rendered: List[str] = []

    def refresh_pngs(self, model_paths, allowed_root=None, force=True, progress=None, **kwargs):
        written: List[Path] = []
        for index, value in enumerate(model_paths, 1):
            relative = str(value)
            if not is_supported_model(relative):
                continue
            if progress is not None:
                progress(index, len(model_paths), relative)
            target = (self.root / relative).with_suffix(".png")
            target.write_bytes(b"\x89PNG\r\n\x1a\n" + relative.encode("utf-8"))
            self.rendered.append(relative)
            written.append(target)
        return {"written": written, "errors": []}


class _FakePanel:
    """Duck-typed panel used to drive SyncController without a real GUI."""

    def __init__(self, repository: GitRepository, root: Path) -> None:
        self.context = None
        self.repository = repository
        self.root = root
        self.settings: Dict[str, Any] = {}
        self.preview = _RecordingPreview(root)
        self.preview_calls: List[List[str]] = []
        self.exit_commit_calls: List[List[str]] = []
        self.confirmations: List[str] = []
        self.allow_confirm = True
        self.finished: List[Dict[str, Any]] = []
        self.failures: List[BaseException] = []
        self.preparing = 0
        # None disables the prompt so the default timestamp message is used.
        self.prompt_answer: Optional[str] = None
        self.prompts: List[List[str]] = []
        self.remembered: List[str] = []
        self.cancelled: List[List[str]] = []

    # -- panel API used by the controller -------------------------------
    def ask_commit_message(self, relative_paths) -> Optional[str]:
        values = [str(value) for value in relative_paths]
        self.prompts.append(values)
        return self.prompt_answer

    def remember_commit_message(self, message: str) -> None:
        self.remembered.append(str(message))
        self.settings["custom_commit_message"] = str(message)

    def commit_message_cancelled(self, relative_paths) -> None:
        self.cancelled.append([str(value) for value in relative_paths])

    def confirm_insecure_operation(self, operation: str) -> bool:
        self.confirmations.append(operation)
        return self.allow_confirm

    def auto_sync_preparing(self) -> None:
        self.preparing += 1

    def auto_sync_finished(self, result: Dict[str, Any]) -> None:
        self.finished.append(result)

    def auto_sync_failed(self, error: BaseException) -> None:
        self.failures.append(error)

    def prepare_commit_previews(self, relative_paths) -> Dict[str, Any]:
        values = sorted({str(path) for path in relative_paths if path})
        self.preview_calls.append(values)
        result = self.preview.refresh_pngs(values, allowed_root=str(self.root), force=True)
        paths = [
            Path(str(target)).resolve().relative_to(self.root.resolve()).as_posix()
            for target in result["written"]
        ]
        return {"paths": paths, "errors": result["errors"]}

    def prepare_exit_commit(self, relative_paths) -> Dict[str, Any]:
        values = sorted({str(path) for path in relative_paths if path})
        self.exit_commit_calls.append(values)
        preview = self.prepare_commit_previews(values)
        commit_paths = sorted(set(values) | set(preview["paths"]))
        commit = None
        if commit_paths:
            commit = self.repository.commit_paths(
                commit_paths,
                build_commit_message(self.settings, values, prefix="Exit save"),
            )
        return {"commit": commit, "previews": preview}


class _Widget:
    """Minimal widget stand-in for assertions about one property."""

    def setEnabled(self, enabled):  # noqa: N802 - Qt API name
        self.enabled = enabled


class _FakeItem:
    def __init__(self, texts):
        self._texts = list(texts)
        self._children = []
        self._expanded = False
        self._parent = None

    def setExpanded(self, expanded):  # noqa: N802 - Qt API name
        self._expanded = expanded

    def isExpanded(self):  # noqa: N802 - Qt API name
        return self._expanded

    def addChild(self, item):  # noqa: N802 - Qt API name
        item._parent = self
        self._children.append(item)

    def parent(self):
        return self._parent

    def text(self, column):  # noqa: N802 - Qt API name
        return self._texts[column]

    def setText(self, column, value):  # noqa: N802 - Qt API name
        self._texts[column] = value

    def childCount(self):  # noqa: N802 - Qt API name
        return len(self._children)

    def children(self):
        return list(self._children)

    def setData(self, _column, _role, value):  # noqa: N802 - Qt API name
        self._data = value

    def data(self, _column, _role):
        return getattr(self, "_data", None)

    def setToolTip(self, *_args):
        pass


class _Flag:
    """Stable stand-in for a QMessageBox button flag.

    The Qt stub hands out a fresh object on every enum lookup, so an answer
    could never compare equal to a later ``message_yes()``, and
    ``message_buttons`` ORs its arguments together.  Real Qt flags do both.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __or__(self, other):
        return _Flag("{}|{}".format(self.name, getattr(other, "name", other)))

    def __repr__(self) -> str:
        return "<flag {}>".format(self.name)


class _QtWithRealItems:
    """QtWidgets stand-in that builds real fakes but stubs the rest.

    ``QTreeWidgetItem`` is the only class the panel constructs rows with, so
    only that one is replaced; every other attribute falls through to the Qt
    stub the rest of the suite relies on.  The fallback target is captured at
    construction: reading ``sync_panel.QtWidgets`` here would recurse into the
    patched attribute itself.
    """

    QTreeWidgetItem = _FakeItem

    def __init__(self, fallback):
        self._fallback = fallback

    def __getattr__(self, name):
        return getattr(self._fallback, name)


class _Signal:
    """Stand-in for a bound Qt signal: .connect(...) then .trigger()."""

    def __init__(self, owner):
        self._owner = owner

    def connect(self, slot):
        self._owner.slots.append(slot)


class _MenuAction:
    def __init__(self, label):
        self.label = label
        self.slots = []
        # PySide exposes `action.triggered` as an attribute, not a method.
        self.triggered = _Signal(self)

    def text(self):
        return self.label

    def trigger(self):
        for slot in self.slots:
            slot()


class _MenuSeparator:
    def text(self):
        return ""


class _Menu:
    """QMenu stand-in recording what the panel builds."""

    built: List[Any] = []

    def __init__(self, *args, **kwargs):
        self._items: List[Any] = []

    def addAction(self, *args):  # noqa: N802 - Qt API name
        action = _MenuAction(args[0] if args and isinstance(args[0], str) else "")
        self._items.append(action)
        return action

    def addSeparator(self):  # noqa: N802 - Qt API name
        self._items.append(_MenuSeparator())
        return None

    def actions(self):
        return list(self._items)

    def exec(self, *args, **kwargs):  # noqa: A003 - Qt API name
        _Menu.built.append(list(self._items))
        return None


# Bound after both classes exist: _QtWithRealItems is defined above _Menu.
_QtWithRealItems.QMenu = _Menu


class _FakeTree:
    """QTreeWidget stand-in recording the rows _populate_models builds."""

    def __init__(self):
        self._top = []

    def blockSignals(self, _value):  # noqa: N802 - Qt API name
        pass

    def clear(self):
        self._top = []

    def addTopLevelItem(self, item):  # noqa: N802 - Qt API name
        self._top.append(item)

    def expandAll(self):  # noqa: N802 - Qt API name
        pass

    def collapseAll(self):  # noqa: N802 - Qt API name
        self.collapsed = True

    def setCurrentItem(self, item):  # noqa: N802 - Qt API name
        self.current = item

    def currentItem(self):  # noqa: N802 - Qt API name
        return getattr(self, "current", None)

    def items(self):
        return list(self._top)

    def itemAt(self, _position):  # noqa: N802 - Qt API name
        return self._top[0] if self._top else None

    def viewport(self):
        return self

    def mapToGlobal(self, position):  # noqa: N802 - Qt API name
        return position


class SyncFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="gitsync-flow-")
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self._git("init", "-b", "main")
        self._git("config", "user.name", "GitSync Test")
        self._git("config", "user.email", "gitsync@example.invalid")
        (self.root / "README.txt").write_text("one\n", encoding="utf-8")
        self._git("add", "README.txt")
        self._git("commit", "-m", "initial")
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self._git("remote", "add", "origin", str(self.remote))
        self._git("push", "-u", "origin", "main")
        self.repository = GitRepository(str(self.root), remote_url=str(self.remote))
        self.settings = {
            "server_url": "https://example.test/repo.git",
            "preview_home": True,
            "auto_commit_save": True,
            "auto_push_save": False,
            "push_on_exit": False,
            "allow_insecure_http": False,
            "auto_commit_message": "timestamp",
            "custom_commit_message": "",
        }
        self.context = _FakeContext(self.repository, self.settings)
        self.panel = _FakePanel(self.repository, self.root)
        self.panel.settings = self.settings
        self.context.panel = self.panel

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            check=True,
            text=True,
            capture_output=True,
        )

    def _make_model(self, name: str, content: str = "model\n") -> Path:
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def _controller(self, panel=None) -> SyncController:
        controller = SyncController(self.context, panel if panel is not None else self.panel)
        return controller

    def _log_subjects(self) -> List[str]:
        return self._git("log", "--pretty=format:%s").stdout.splitlines()

    def _remote_subjects(self) -> List[str]:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%s", "main"],
            cwd=str(self.remote),
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.splitlines()

    # ------------------------------------------------------------------
    # automatic save
    # ------------------------------------------------------------------
    def test_auto_save_commits_model_and_preview_in_one_timestamped_commit(self) -> None:
        model = self._make_model("StorageBin_Rack.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Rack", str(model)), str(model))
        controller._drain_auto_queue()

        self.assertEqual(self.panel.failures, [])
        self.assertEqual(self.panel.preview_calls, [["StorageBin_Rack.FCStd"]])
        subjects = self._log_subjects()
        self.assertEqual(len(subjects), 2, subjects)
        message = subjects[0]
        self.assertTrue(message.startswith("Auto-save "), message)
        self.assertIn("StorageBin_Rack.FCStd", message)
        # The message carries an ISO-like local timestamp.
        stamp = message[len("Auto-save "):].split(" - ")[0]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        committed = self._git("show", "--name-only", "--pretty=format:").stdout.split()
        self.assertIn("StorageBin_Rack.FCStd", committed)
        self.assertIn("StorageBin_Rack.png", committed)
        self.assertTrue((self.root / "StorageBin_Rack.png").is_file())
        self.assertEqual(len(self.panel.finished), 1)
        self.assertTrue(self.panel.finished[0]["commit"]["created"])
        # No separate preview-only commit is produced.
        self.assertFalse(any("preview" in subject.lower() for subject in subjects))

    def test_auto_save_pushes_plainly_without_extra_preview_commit(self) -> None:
        self.settings["auto_push_save"] = True
        model = self._make_model("Bracket.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Bracket", str(model)), str(model))
        controller._drain_auto_queue()

        self.assertEqual(self.panel.failures, [])
        self.assertEqual(len(self._log_subjects()), 2)
        self.assertTrue(self.panel.finished[0]["push"], "expected the automatic push to run")
        self.assertEqual(self._remote_subjects(), self._log_subjects())

    def test_auto_save_uses_the_message_typed_in_the_prompt(self) -> None:
        self.settings["auto_commit_message"] = "custom"
        self.panel.prompt_answer = "Workshop update"
        model = self._make_model("Rack.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Rack", str(model)), str(model))
        controller._drain_auto_queue()
        self.assertEqual(self.panel.failures, [])
        self.assertEqual(self.panel.prompts, [["Rack.FCStd"]])
        self.assertEqual(self._log_subjects()[0], "Workshop update")
        # The message is remembered for the prefill and the exit commit.
        self.assertEqual(self.panel.remembered, ["Workshop update"])
        self.assertEqual(self.settings["custom_commit_message"], "Workshop update")
        committed = self._git("show", "--name-only", "--pretty=format:").stdout.split()
        self.assertEqual(sorted(committed), ["Rack.FCStd", "Rack.png"])

    def test_empty_prompt_falls_back_to_the_timestamp(self) -> None:
        self.settings["auto_commit_message"] = "custom"
        self.panel.prompt_answer = "   "
        model = self._make_model("Rack.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Rack", str(model)), str(model))
        controller._drain_auto_queue()
        self.assertEqual(self.panel.remembered, [])
        self.assertRegex(self._log_subjects()[0], r"^Auto-save \d{4}-\d{2}-\d{2} ")

    def test_cancelling_the_prompt_leaves_the_document_uncommitted(self) -> None:
        self.settings["auto_commit_message"] = "custom"
        self.panel.prompt_answer = None
        model = self._make_model("Rack.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Rack", str(model)), str(model))
        controller._drain_auto_queue()
        self.assertEqual(self.panel.cancelled, [["Rack.FCStd"]])
        self.assertEqual(self.panel.finished, [])
        self.assertEqual(len(self._log_subjects()), 1)
        self.assertEqual(self.repository.status()["entries"][0]["path"], "Rack.FCStd")
        # Nothing was rendered either, because the prompt comes first.
        self.assertEqual(self.panel.preview_calls, [])

    def test_exit_commit_uses_the_remembered_message(self) -> None:
        self.settings["auto_commit_save"] = False
        self.settings["push_on_exit"] = True
        self.settings["auto_commit_message"] = "custom"
        self.settings["custom_commit_message"] = "Session close"
        model = self._make_model("Rack.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Rack", str(model)), str(model))
        controller.push_on_exit(force=True, final=True)
        # No prompt is possible at exit, so the remembered message is used.
        self.assertEqual(self.panel.prompts, [])
        self.assertEqual(self._log_subjects()[0], "Session close")

    def test_auto_save_does_not_render_previews_for_unrelated_models(self) -> None:
        untouched = self._make_model("Untouched.step", "solid\n")
        model = self._make_model("Saved.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Saved", str(model)), str(model))
        controller._drain_auto_queue()

        self.assertEqual(self.panel.preview.rendered, ["Saved.FCStd"])
        self.assertFalse((untouched.with_suffix(".png")).exists())

    def test_auto_save_is_skipped_when_disabled_but_tracked_for_exit(self) -> None:
        self.settings["auto_commit_save"] = False
        self.settings["push_on_exit"] = True
        model = self._make_model("Manual.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Manual", str(model)), str(model))
        controller._drain_auto_queue()

        self.assertEqual(self.panel.preview_calls, [])
        self.assertEqual(len(self._log_subjects()), 1)
        controller.push_on_exit(force=True, final=True)
        self.assertEqual(self.panel.exit_commit_calls, [["Manual.FCStd"]])
        subjects = self._log_subjects()
        self.assertEqual(len(subjects), 2, subjects)
        self.assertTrue(subjects[0].startswith("Exit save"), subjects[0])
        self.assertEqual(self._remote_subjects(), subjects)

    # ------------------------------------------------------------------
    # exit
    # ------------------------------------------------------------------
    def test_exit_commits_only_session_documents_and_never_scans_models(self) -> None:
        self.settings["push_on_exit"] = True
        self.settings["auto_commit_save"] = False
        first = self._make_model("First.FCStd")
        second = self._make_model("Second.step", "solid\n")
        # A file the user never opened stays untracked and uncommitted.
        untouched = self._make_model("Untouched.stl", "solid\n")

        original_model_files = self.repository.model_files

        def explode():  # pragma: no cover - the guard we assert against
            raise AssertionError("exit must not scan the repository for models")

        self.repository.model_files = explode
        try:
            controller = self._controller()
            controller.document_saved(_FakeDocument("First", str(first)), str(first))
            controller.document_saved(_FakeDocument("Second", str(second)), str(second))
            controller.push_on_exit(force=True, final=True)
        finally:
            self.repository.model_files = original_model_files

        self.assertEqual(self.panel.failures, [])
        self.assertEqual(
            self.panel.exit_commit_calls,
            [["First.FCStd", "Second.step"]],
        )
        self.assertEqual(self.panel.preview.rendered, ["First.FCStd", "Second.step"])
        subjects = self._log_subjects()
        self.assertEqual(len(subjects), 2, subjects)
        committed = self._git("show", "--name-only", "--pretty=format:").stdout.split()
        self.assertIn("First.FCStd", committed)
        self.assertIn("First.png", committed)
        self.assertIn("Second.step", committed)
        self.assertIn("Second.png", committed)
        self.assertNotIn("Untouched.stl", committed)
        self.assertTrue(untouched.exists())
        self.assertEqual(self._remote_subjects(), subjects)

    def test_exit_does_not_recommit_documents_handled_by_the_auto_commit(self) -> None:
        self.settings["push_on_exit"] = True
        model = self._make_model("Auto.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Auto", str(model)), str(model))
        # The automatic commit runs first, exactly like the save debounce does.
        controller._drain_auto_queue()
        controller.push_on_exit(force=True, final=True)

        self.assertEqual(self.panel.exit_commit_calls, [])
        self.assertEqual(len(self._log_subjects()), 2)
        self.assertTrue(self._log_subjects()[0].startswith("Auto-save "))
        self.assertEqual(self._remote_subjects(), self._log_subjects())

    def test_forget_session_paths_clears_only_the_given_paths(self) -> None:
        self.settings["push_on_exit"] = True
        first = self._make_model("One.FCStd")
        second = self._make_model("Two.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("One", str(first)), str(first))
        controller.document_saved(_FakeDocument("Two", str(second)), str(second))
        controller.forget_session_paths(["One.FCStd"])
        self.assertEqual(controller._exit_commit_paths(), {"Two.FCStd"})
        controller.forget_session_paths()
        self.assertEqual(controller._exit_commit_paths(), set())

    def test_exit_includes_open_modified_documents(self) -> None:
        self.settings["push_on_exit"] = True
        model = self._make_model("Open.FCStd")
        controller = self._controller()

        import gitsync.sync_events as sync_events

        original_app = sync_events.App
        sync_events.App = type(
            "FakeApp",
            (),
            {"listDocuments": staticmethod(lambda: {"Open": _FakeDocument("Open", str(model), True)})},
        )
        try:
            controller.push_on_exit(force=True, final=True)
        finally:
            sync_events.App = original_app

        self.assertEqual(self.panel.exit_commit_calls, [["Open.FCStd"]])

    def test_exit_never_runs_when_setting_is_disabled(self) -> None:
        model = self._make_model("Ignored.FCStd")
        controller = self._controller()
        controller.document_saved(_FakeDocument("Ignored", str(model)), str(model))
        controller.push_on_exit(force=True, final=True)
        self.assertEqual(self.panel.exit_commit_calls, [])
        self.assertEqual(len(self._log_subjects()), 1)

    # ------------------------------------------------------------------
    # panel-level preview preparation
    # ------------------------------------------------------------------
    def _bare_panel(self) -> SyncPanel:
        panel = SyncPanel.__new__(SyncPanel)
        panel.context = self.context
        panel._preview = _RecordingPreview(self.root)
        panel._statuses = []
        panel._set_status = lambda text: panel._statuses.append(text)
        return panel

    def test_commit_previews_only_cover_the_included_models(self) -> None:
        self._make_model("A.FCStd")
        self._make_model("B.step", "solid\n")
        panel = self._bare_panel()
        result = panel.prepare_commit_previews(["A.FCStd", "notes.txt"])
        self.assertEqual(result["paths"], ["A.png"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(panel._preview.rendered, ["A.FCStd"])
        self.assertTrue((self.root / "A.png").is_file())
        self.assertFalse((self.root / "B.png").exists())

    def test_commit_previews_remove_the_png_of_a_deleted_model(self) -> None:
        model = self._make_model("Gone.FCStd")
        (self.root / "Gone.png").write_bytes(b"old preview")
        panel = self._bare_panel()
        result = panel.prepare_commit_previews(["Gone.FCStd"])
        self.assertEqual(result["paths"], ["Gone.png"])
        self.assertEqual(result["errors"], [])

    def test_prepare_exit_commit_stages_model_and_preview_together(self) -> None:
        model = self._make_model("Rack.FCStd", "changed\n")
        (self.root / "unrelated.txt").write_text("leave me\n", encoding="utf-8")
        panel = self._bare_panel()
        result = panel.prepare_exit_commit(["Rack.FCStd"])
        self.assertTrue(result["commit"]["created"])
        self.assertTrue(result["commit"]["message"].startswith("Exit save"))
        committed = self._git("show", "--name-only", "--pretty=format:").stdout.split()
        self.assertEqual(sorted(committed), ["Rack.FCStd", "Rack.png"])
        self.assertTrue(
            (self.root / "unrelated.txt").exists(),
            "the exit commit must not stage unrelated files",
        )

    # ------------------------------------------------------------------
    # new document creation
    # ------------------------------------------------------------------
    def test_new_model_target_validates_the_name(self) -> None:
        panel = self._bare_panel()
        filename, target = panel._new_model_target("new part")
        self.assertEqual(filename, "new part.FCStd")
        self.assertEqual(target, self.root / "new part.FCStd")
        # Validating the name must not create anything.
        self.assertFalse(target.exists())
        self.assertEqual(panel._default_new_model_name(), "NewModel")

    def test_new_model_target_refuses_existing_and_unsafe_names(self) -> None:
        panel = self._bare_panel()
        self._make_model("Rack.FCStd")
        with self.assertRaises(GitError):
            panel._new_model_target("Rack")
        # A case-folding file system would overwrite the existing document.
        with self.assertRaises(GitError):
            panel._new_model_target("rack")
        with self.assertRaises(GitError):
            panel._new_model_target("sub/part")
        # A dot is part of the name now, so "Rack.step" is a legal name and
        # becomes "Rack.step.FCStd"; only separators are refused.
        filename, _target = panel._new_model_target("Rack.step")
        self.assertEqual(filename, "Rack.step.FCStd")
        with self.assertRaises(GitError):
            panel._new_model_target("Rack:alt")

    def test_new_model_suggests_the_next_free_name(self) -> None:
        panel = self._bare_panel()
        self._make_model("NewModel.FCStd")
        self._make_model("NewModel2.FCStd")
        self.assertEqual(panel._default_new_model_name(), "NewModel3")

    def test_the_suggested_name_hides_the_extension(self) -> None:
        panel = self._bare_panel()
        # The prompt shows what the user edits; the suffix is added on
        # creation, so it must not be in the suggested name.
        self.assertFalse(panel._default_new_model_name().endswith(".FCStd"))
        self._make_model("NewModel.FCStd")
        for index in range(1, 6):
            suggestion = panel._default_new_model_name()
            self.assertNotIn(".", suggestion)
            self._make_model(suggestion + ".FCStd")

    def test_a_taken_suggestion_is_skipped_by_stem_not_by_full_name(self) -> None:
        panel = self._bare_panel()
        # The existing file carries the suffix; the suggestion does not, so
        # comparing full names would happily suggest a name that is taken.
        self._make_model("NewModel.FCStd")
        self.assertEqual(panel._default_new_model_name(), "NewModel2")

    def test_typing_the_extension_yet_also_works(self) -> None:
        panel = self._bare_panel()
        # Tolerant in both directions: the prompt hides the suffix, but a user
        # who types it must not end up with "Rack.FCStd.FCStd".
        with_suffix, _target = panel._new_model_target("Rack.FCStd")
        without, _target = panel._new_model_target("Rack")
        self.assertEqual(with_suffix, "Rack.FCStd")
        self.assertEqual(without, "Rack.FCStd")
        self.assertEqual(with_suffix, without)

    def test_commit_prompt_prefills_the_file_name(self) -> None:
        panel = self._bare_panel()
        prompts = []
        panel._ask_text = lambda title, label, initial: prompts.append((label, initial)) or "done"
        self.assertEqual(panel.ask_commit_message(["Rack.FCStd"]), "done")
        self.assertEqual(prompts[-1][0], "Commit message for Rack.FCStd:")
        self.assertEqual(prompts[-1][1], "Rack.FCStd")

    def test_commit_prompt_prefills_the_remembered_message(self) -> None:
        self.context.settings["custom_commit_message"] = "Workshop update"
        panel = self._bare_panel()
        prompts = []
        panel._ask_text = lambda title, label, initial: prompts.append((label, initial)) or "ok"
        panel.ask_commit_message(["Rack.FCStd"])
        self.assertEqual(prompts[-1][1], "Workshop update")
        panel.ask_commit_message(["a.step", "b.FCStd"])
        self.assertEqual(prompts[-1][0], "Commit message for 2 documents:")

    def test_commit_prompt_reports_cancellation(self) -> None:
        panel = self._bare_panel()
        panel._ask_text = lambda title, label, initial: None
        self.assertIsNone(panel.ask_commit_message(["Rack.FCStd"]))

    def test_remember_commit_message_persists_the_text(self) -> None:
        panel = self._bare_panel()
        panel.remember_commit_message("  Workshop update  ")
        self.assertEqual(self.context.settings["custom_commit_message"], "Workshop update")
        self.assertEqual(
            self.context.settings_store.load()["custom_commit_message"],
            "Workshop update",
        )

    # ------------------------------------------------------------------
    # push
    # ------------------------------------------------------------------
    def test_push_is_a_plain_push_without_preview_work(self) -> None:
        panel = self._bare_panel()
        submitted = []
        panel._submit = lambda function, *args, **kwargs: submitted.append(function)
        original_model_files = self.repository.model_files

        def explode():  # pragma: no cover - the guard we assert against
            raise AssertionError("push must not scan the repository for models")

        self.repository.model_files = explode
        try:
            panel.push()
        finally:
            self.repository.model_files = original_model_files
        self.assertEqual(submitted, [self.repository.push])
        self.assertEqual(panel._preview.rendered, [])
        self.assertTrue(self.panel.preview.rendered == [])

    # ------------------------------------------------------------------
    # repository trust for .FCStd documents
    # ------------------------------------------------------------------
    def test_untrusted_repository_asks_before_previewing_an_fcstd(self) -> None:
        panel = self._bare_panel()
        prompts = []
        panel._preview_warning_shown = False
        original = sync_panel.show_trust_prompt
        sync_panel.show_trust_prompt = lambda *args, **kwargs: prompts.append(args) or "no"
        try:
            self.assertFalse(panel._confirm_fcstd_preview("Rack.FCStd"))
            self.assertFalse(panel._confirm_fcstd_preview("Rack.FCStd"))
        finally:
            sync_panel.show_trust_prompt = original
        self.assertEqual(len(prompts), 2)
        # Other formats are not affected by the document warning.
        self.assertTrue(panel._confirm_fcstd_preview("Bracket.step"))

    def test_yes_only_trusts_the_current_session(self) -> None:
        panel = self._bare_panel()
        panel._preview_warning_shown = False
        original = sync_panel.show_trust_prompt
        sync_panel.show_trust_prompt = lambda *args, **kwargs: "yes"
        try:
            self.assertTrue(panel._confirm_fcstd_preview("Rack.FCStd"))
            panel._preview_warning_shown = False
            self.assertTrue(panel._confirm_fcstd_preview("Other.FCStd"))
        finally:
            sync_panel.show_trust_prompt = original
        self.assertEqual(self.context.settings.get("trusted_repository", ""), "")

    def test_trusting_a_repository_persists_and_skips_future_prompts(self) -> None:
        from gitsync.git_service import canonical_remote_target

        self.context.settings["server_url"] = "https://example.test/repo.git"
        self.context.settings["username"] = "user"
        panel = self._bare_panel()
        panel._preview_warning_shown = False
        target = canonical_remote_target(
            self.context.settings["server_url"], self.context.settings["username"]
        )

        original = sync_panel.show_trust_prompt
        prompts = []
        sync_panel.show_trust_prompt = lambda *args, **kwargs: prompts.append(args) or "trust"
        try:
            self.assertTrue(panel._confirm_fcstd_preview("Rack.FCStd"))
        finally:
            sync_panel.show_trust_prompt = original
        self.assertEqual(len(prompts), 1)
        self.assertEqual(self.context.settings.get("trusted_repository"), target)

        # A later preview does not ask again, even for another document.
        panel._preview_warning_shown = False
        self.assertTrue(panel._confirm_fcstd_preview("Other.FCStd"))
        self.assertTrue(panel._confirm_fcstd_preview("open_anything.FCStd"))

        # The stored decision is bound to that repository only.
        self.context.settings["server_url"] = "https://other.test/repo.git"
        self.assertFalse(panel._repository_is_trusted())
        panel._preview_warning_shown = False
        prompts.clear()
        sync_panel.show_trust_prompt = lambda *args, **kwargs: prompts.append(args) or "no"
        try:
            self.assertFalse(panel._confirm_fcstd_preview("Rack.FCStd"))
        finally:
            sync_panel.show_trust_prompt = original
        self.assertEqual(len(prompts), 1)

    def test_revoking_trust_restores_the_prompt(self) -> None:
        self.context.settings["server_url"] = "https://example.test/repo.git"
        panel = self._bare_panel()
        panel.trust_current_repository(True)
        self.assertTrue(panel._repository_is_trusted())
        panel.trust_current_repository(False)
        self.assertFalse(panel._repository_is_trusted())
        self.assertEqual(self.context.settings.get("trusted_repository", ""), "")

    # ------------------------------------------------------------------
    # model list columns
    # ------------------------------------------------------------------
    def test_modified_column_is_narrower_than_a_date_with_a_time(self) -> None:
        """The date column must not claim width the name column needs."""
        panel = self._bare_panel()
        calls = {"mode": None, "width": None}

        class _Metrics:
            def horizontalAdvance(self, text):  # noqa: N802 - Qt API name
                return 7 * len(text)

        class _Header:
            def setSectionResizeMode(self, section, mode):  # noqa: N802
                calls["mode"] = (section, mode)

            def resizeSection(self, section, width):  # noqa: N802
                calls["width"] = width

            def setStretchLastSection(self, enabled):  # noqa: N802
                calls["stretch_last"] = enabled

        class _Tree:
            header = staticmethod(lambda: _Header())

            def fontMetrics(self):  # noqa: N802 - Qt API name
                return _Metrics()

        panel.models_tree = _Tree()
        calls["stretch_last"] = "unset"
        # The Qt stub mints a fresh enum object per access, so identity cannot
        # be compared; check that the panel passes through the fixed-mode
        # helper instead (ResizeToContents would ignore the set width).
        sentinel = object()
        original = sync_panel.header_fixed_mode
        sync_panel.header_fixed_mode = lambda: sentinel
        try:
            panel._size_modified_column()
        finally:
            sync_panel.header_fixed_mode = original
        # stretchLastSection defaults to True and overrides the Fixed mode,
        # which silently gave the last column half the width.
        self.assertIs(calls["stretch_last"], False)
        self.assertEqual(calls["mode"][0], 1)
        self.assertIs(calls["mode"][1], sentinel)
        # "Modified" is the widest thing in the header; the padding covers the
        # cell margins.  A date with a time appended would be far wider.
        self.assertLess(calls["width"], len("2026-01-31 14:05") * 7)
        self.assertGreater(calls["width"], len("Modified") * 7)

    def test_model_column_keeps_the_stretch_mode(self) -> None:
        source = inspect.getsource(SyncPanel._build_ui)
        self.assertIn("setSectionResizeMode(0, header_stretch_mode())", source)

    def test_dark_palette_gets_gitsyncs_own_branch_arrows(self) -> None:
        """A dark theme's own glyph was measured at 1.5:1 - all but invisible."""
        panel = self._bare_panel()
        sheets = []

        class _Tree:
            def palette(self):
                return "dark"

            def setStyleSheet(self, text):  # noqa: N802 - Qt API name
                sheets.append(text)

        panel.models_tree = _Tree()
        original = sync_panel.palette_is_dark
        sync_panel.palette_is_dark = lambda _palette: True
        try:
            panel._apply_branch_indicator()
        finally:
            sync_panel.palette_is_dark = original
        self.assertEqual(len(sheets), 1)
        sheet = sheets[0]
        # Both arrows, and every state Qt can ask for.
        self.assertIn("tree-branch-closed.svg", sheet)
        self.assertIn("tree-branch-open.svg", sheet)
        self.assertIn(":closed:has-children:has-siblings", sheet)
        self.assertIn(":has-children:!has-siblings:closed", sheet)
        self.assertIn(":open:has-children:has-siblings", sheet)
        self.assertIn(":open:has-children:!has-siblings", sheet)
        # The referenced icons have to exist, or the arrow is silently lost.
        fills = []
        for name in ("tree-branch-closed.svg", "tree-branch-open.svg"):
            path = Path(sync_panel.__file__).resolve().parent.parent / "icons" / name
            self.assertTrue(path.is_file(), path)
            source = path.read_text(encoding="utf-8")
            # A solid fill with no transparency: a faint or see-through arrow is
            # exactly the problem these icons exist to solve.
            self.assertIn('fill="#', source)
            self.assertNotIn("opacity", source)
            self.assertNotIn("fill-opacity", source)
            fills.append(source.split('fill="', 1)[1].split('"', 1)[0])
        # Both states must match, or the tree flips colour when expanding.
        self.assertEqual(fills[0], fills[1])

    def test_light_palette_is_handed_back_to_the_theme(self) -> None:
        panel = self._bare_panel()
        sheets = []

        class _Tree:
            def palette(self):
                return "light"

            def setStyleSheet(self, text):  # noqa: N802 - Qt API name
                sheets.append(text)

        panel.models_tree = _Tree()
        original = sync_panel.palette_is_dark
        sync_panel.palette_is_dark = lambda _palette: False
        try:
            panel._apply_branch_indicator()
        finally:
            sync_panel.palette_is_dark = original
        # Cleared, not skipped, so a palette change cannot leave it stuck on.
        self.assertEqual(sheets, [""])

    def test_a_folder_row_shows_the_date_it_sorts_by(self) -> None:
        """A folder's Modified cell must show what its position is based on."""
        panel = self._bare_panel()
        panel.models_tree = _FakeTree()
        panel._select_after_refresh = None
        panel._selected_model_path = lambda: None
        panel._model_filter = ""
        panel._sort_order = lambda: MODIFIED_SORT
        panel.open_model_button = _Widget()

        models = ["middle.FCStd", "fresh/new.FCStd", "fresh/old.FCStd"]
        times = {
            "middle.FCStd": 500.0,
            "fresh/new.FCStd": 9000.0,
            "fresh/old.FCStd": 200.0,
        }
        # The Qt stub answers every widget call with a placeholder, so swap in
        # real fakes for the item type the panel constructs its rows with.
        original = sync_panel.QtWidgets
        sync_panel.QtWidgets = _QtWithRealItems(original)
        try:
            panel._populate_models(models, times)
        finally:
            sync_panel.QtWidgets = original

        rows = [(item.text(0), item.text(1), item.childCount())
                for item in panel.models_tree.items()]
        self.assertEqual([row[0] for row in rows], ["fresh", "middle.FCStd"])
        # The folder leads the list because of this date, so it has to be shown.
        self.assertEqual(rows[0][1], format_timestamp(9000.0))
        self.assertEqual(rows[0][2], 2)
        self.assertEqual(rows[1][1], format_timestamp(500.0))
        self.assertEqual(rows[1][2], 0)
        # Children keep their own dates.
        children = [(child.text(0), child.text(1))
                    for child in panel.models_tree.items()[0].children()]
        self.assertEqual(
            children,
            [("new.FCStd", format_timestamp(9000.0)),
             ("old.FCStd", format_timestamp(200.0))],
        )

    def test_folders_start_collapsed(self) -> None:
        panel = self._bare_panel()
        panel.models_tree = _FakeTree()
        panel._select_after_refresh = None
        panel._selected_model_path = lambda: None
        panel._model_filter = ""
        panel._sort_order = lambda: NAME_SORT
        panel.open_model_button = _Widget()

        original = sync_panel.QtWidgets
        sync_panel.QtWidgets = _QtWithRealItems(original)
        try:
            panel._populate_models(["a/one.FCStd", "b.FCStd"], {"a/one.FCStd": 1.0})
        finally:
            sync_panel.QtWidgets = original

        self.assertTrue(panel.models_tree.collapsed)
        folder = panel.models_tree.items()[0]
        self.assertEqual(folder.text(0), "a")
        self.assertFalse(folder.isExpanded())

    def test_restoring_a_selection_opens_the_folders_above_it(self) -> None:
        """A collapsed folder must not hide the model it holds."""
        panel = self._bare_panel()
        panel.models_tree = _FakeTree()
        panel._model_filter = ""
        panel._sort_order = lambda: NAME_SORT
        panel.open_model_button = _Widget()
        panel._preview = _RecordingPreview(self.root)

        original = sync_panel.QtWidgets
        sync_panel.QtWidgets = _QtWithRealItems(original)
        try:
            panel._select_after_refresh = "a/one.FCStd"
            panel._populate_models(["a/one.FCStd"], {"a/one.FCStd": 1.0})
            folder = panel.models_tree.items()[0]
            self.assertTrue(folder.isExpanded())
            self.assertEqual(
                panel.models_tree.current, folder.children()[0]
            )
            # A rebuild whose model is no longer listed has nothing to reveal,
            # so its folder is left collapsed.
            panel._select_after_refresh = None
            panel.models_tree.current = None
            panel._populate_models(["b/other.FCStd"], {"b/other.FCStd": 2.0})
            self.assertFalse(panel.models_tree.items()[0].isExpanded())
        finally:
            sync_panel.QtWidgets = original

    def test_file_browser_refuses_a_path_outside_the_workspace(self) -> None:
        """The action must go through the shared path checks, not trust the tree."""
        panel = self._bare_panel()
        panel._selected_model_path = lambda: "../outside.FCStd"
        panel.context.repository = _Repository(self.root)
        launched = []
        errors = []
        original = sync_panel.reveal_in_file_manager
        original_message = sync_panel.show_plain_message
        sync_panel.reveal_in_file_manager = lambda path: launched.append(path) or "cmd"
        sync_panel.show_plain_message = lambda *args: errors.append(args)
        try:
            panel.open_selected_in_file_manager()
        finally:
            sync_panel.reveal_in_file_manager = original
            sync_panel.show_plain_message = original_message
        # Nothing reached the file manager, and the refusal was reported.
        self.assertEqual(launched, [])
        self.assertTrue(errors)

    def test_file_browser_launches_for_a_valid_model(self) -> None:
        panel = self._bare_panel()
        model = self.root / "Rack.FCStd"
        model.write_bytes(b"x")
        panel._selected_model_path = lambda: "Rack.FCStd"
        panel.context.repository = _Repository(self.root)
        launched = []
        original = sync_panel.reveal_in_file_manager
        sync_panel.reveal_in_file_manager = lambda path: launched.append(path) or "cmd"
        panel._set_status = lambda text: None
        try:
            panel.open_selected_in_file_manager()
        finally:
            sync_panel.reveal_in_file_manager = original
        self.assertEqual(launched, [model.resolve()])

    def test_context_menu_offers_all_four_actions(self) -> None:
        panel = self._bare_panel()
        panel.models_tree = _FakeTree()
        _Menu.built.clear()

        model = self.root / "Rack.FCStd"
        model.write_bytes(b"x")
        panel._model_paths = ["Rack.FCStd"]
        panel._model_mtimes = {"Rack.FCStd": 1.0}
        panel._model_filter = ""
        panel._select_after_refresh = None
        panel._selected_model_path = lambda: None
        panel._sort_order = lambda: NAME_SORT
        panel.open_model_button = _Widget()
        panel.context.repository = _Repository(self.root)

        original = sync_panel.QtWidgets
        sync_panel.QtWidgets = _QtWithRealItems(original)
        try:
            panel._populate_models(panel._model_paths, panel._model_mtimes)
            item = panel.models_tree.items()[0]
            panel._model_context_menu(item)
        finally:
            sync_panel.QtWidgets = original

        self.assertEqual(len(_Menu.built), 1)
        labels = [action.text() for action in _Menu.built[0]]
        self.assertEqual(
            labels,
            ["Open Model", "Open in File Browser", "", "Rename…", "Delete"],
        )
        # The destructive actions are last, behind a separator.
        self.assertEqual(labels[-1], "Delete")
        separator = labels.index("")
        self.assertEqual(labels[separator:], ["", "Rename…", "Delete"])

    def test_delete_is_refused_while_the_document_is_open(self) -> None:
        panel = self._bare_panel()
        model = self.root / "Rack.FCStd"
        model.write_bytes(b"x")
        panel._selected_model_path = lambda: "Rack.FCStd"
        panel.context.repository = _Repository(self.root)
        panel._document_open_at = lambda path: True
        answers = []
        original = sync_panel.show_plain_message
        sync_panel.show_plain_message = lambda *args: answers.append(args) or "no"
        try:
            panel.delete_selected_model()
        finally:
            sync_panel.show_plain_message = original
        self.assertTrue(model.is_file())
        self.assertEqual(len(answers), 1)
        self.assertIn("Close Rack.FCStd", str(answers[0]))

    def test_delete_needs_an_explicit_yes(self) -> None:
        panel = self._bare_panel()
        model = self.root / "Rack.FCStd"
        model.write_bytes(b"x")
        preview = self.root / "Rack.png"
        preview.write_bytes(b"png")
        repository = _Repository(self.root)
        panel._selected_model_path = lambda: "Rack.FCStd"
        panel.context.repository = repository
        panel._document_open_at = lambda path: False
        panel._set_status = lambda text: None
        panel.refresh = lambda **kwargs: None

        messages = []

        def decline(*args):
            messages.append(args)
            return no

        # The Qt stub hands out a *fresh* object for every enum lookup, so
        # `answer != message_yes()` in the panel could never be true.  Real
        # enums compare by value; pinning them here reproduces that.
        yes, no = _Flag("yes"), _Flag("no")
        original = sync_panel.show_plain_message
        original_yes = sync_panel.message_yes
        original_no = sync_panel.message_no
        sync_panel.message_yes = lambda: yes
        sync_panel.message_no = lambda: no
        sync_panel.show_plain_message = decline
        try:
            panel.delete_selected_model()
            # Declining leaves everything alone.
            self.assertEqual(repository.removed, [])
            self.assertTrue(model.is_file())
            self.assertTrue(preview.is_file())
            self.assertEqual(len(messages), 1)
            # Agreeing asks Git to remove the model and its preview, and to
            # commit it under a timestamped message.
            messages.clear()
            sync_panel.show_plain_message = (
                lambda *args: messages.append(args) or yes
            )
            panel.delete_selected_model()
            self.assertEqual(len(messages), 1, messages)
        finally:
            sync_panel.show_plain_message = original
            sync_panel.message_yes = original_yes
            sync_panel.message_no = original_no

        self.assertEqual(len(repository.removed), 1)
        paths, message = repository.removed[0]
        self.assertEqual(paths, ["Rack.FCStd", "Rack.png"])
        self.assertTrue(message.endswith(" deleted"), message)
        # The date and time come first, as "{date} {time} deleted".
        stamp = message.split(" deleted")[0]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # The panel itself never unlinks anything.
        self.assertTrue(model.is_file())
        self.assertTrue(preview.is_file())

    def test_rename_asks_git_to_move_the_model_and_its_preview(self) -> None:
        panel = self._bare_panel()
        model = self.root / "Rack.FCStd"
        model.write_bytes(b"x")
        preview = self.root / "Rack.png"
        preview.write_bytes(b"png")
        repository = _Repository(self.root)
        panel._selected_model_path = lambda: "Rack.FCStd"
        panel.context.repository = repository
        panel._document_open_at = lambda path: False
        panel._set_status = lambda text: None
        panel.refresh = lambda **kwargs: None

        yes, no = _Flag("yes"), _Flag("no")
        original_input = sync_panel.QtWidgets.QInputDialog
        original_yes = sync_panel.message_yes
        original_no = sync_panel.message_no

        class _Input:
            @staticmethod
            def getText(*args, **kwargs):
                return "Shelf.FCStd", True

        sync_panel.QtWidgets.QInputDialog = _Input
        sync_panel.message_yes = lambda: yes
        sync_panel.message_no = lambda: no
        try:
            panel.rename_selected_model()
        finally:
            sync_panel.QtWidgets.QInputDialog = original_input
            sync_panel.message_yes = original_yes
            sync_panel.message_no = original_no

        self.assertEqual(len(repository.moved), 1)
        moves, message = repository.moved[0]
        self.assertEqual(
            [list(pair) for pair in moves],
            [["Rack.FCStd", "Shelf.FCStd"], ["Rack.png", "Shelf.png"]],
        )
        self.assertTrue(message.endswith(" renamed"), message)
        stamp = message.split(" renamed")[0]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # The new name is selected once the refreshed list arrives.
        self.assertEqual(panel._select_after_refresh, "Shelf.FCStd")

    def test_qt_url_escapes_a_stylesheet_hostile_path(self) -> None:
        self.assertEqual(
            sync_panel._qt_url("/tmp/a)b/tree-branch-closed.svg"),
            "/tmp/a\\)b/tree-branch-closed.svg",
        )
        self.assertEqual(
            sync_panel._qt_url("/tmp/plain/tree-branch-closed.svg"),
            "/tmp/plain/tree-branch-closed.svg",
        )

    def test_refresh_re_arms_and_runs_the_startup_check(self) -> None:
            """Refresh must behave like a fresh start, not just a snapshot."""
            panel = self._bare_panel()
            calls = []
            panel._startup_checked = True          # already used up at start-up
            panel.refresh = lambda on_success=None, warn_insecure=True: calls.append(
                (on_success, warn_insecure)
            )
            panel.refresh_and_check()
            # The one-shot guard is cleared, and the check is the success callback.
            self.assertFalse(panel._startup_checked)
            self.assertEqual(len(calls), 1)
            # Bound methods are recreated on each access, so compare rather
            # than assert identity.
            self.assertEqual(calls[0][0], panel._startup_check_after_refresh)

    def test_refresh_button_is_wired_to_the_checking_refresh(self) -> None:
        source = inspect.getsource(SyncPanel._build_ui)
        self.assertIn(
            "self.refresh_button.clicked.connect("
            "lambda _checked=False: self.refresh_and_check())",
            " ".join(source.split()),
        )

    def test_refresh_insecure_consent_is_asked_once_per_check(self) -> None:
        # A declined consent must not stop the snapshot itself.
        panel = self._bare_panel()
        asked = []
        panel._repository = lambda: object()
        panel._confirm_insecure_operation = lambda label: asked.append(label) or False
        panel._set_status = lambda text: None
        submitted = []
        panel._submit = lambda function, *a, **kw: submitted.append(function)
        panel._startup_checked = False
        panel._startup_check_after_refresh({})
        self.assertEqual(asked, ["Remote refresh"])
        # The check stopped before submitting the fetch, as intended.
        self.assertEqual(submitted, [])


class _Color:
    def __init__(self, r, g, b):
        self._rgb = (r / 255.0, g / 255.0, b / 255.0)

    def redF(self):  # noqa: N802 - Qt API name
        return self._rgb[0]

    def greenF(self):  # noqa: N802 - Qt API name
        return self._rgb[1]

    def blueF(self):  # noqa: N802 - Qt API name
        return self._rgb[2]


class _Palette:
    def __init__(self, base):
        self._base = base

    def color(self, *_args):
        return self._base


class PaletteTests(unittest.TestCase):
    """The dark-theme check that decides whether GitSync ships its arrows."""

    def test_black_and_white_endpoints(self) -> None:
        self.assertAlmostEqual(relative_luminance(_Color(0, 0, 0)), 0.0)
        self.assertAlmostEqual(relative_luminance(_Color(255, 255, 255)), 1.0)

    def test_a_dark_background_counts_as_dark(self) -> None:
        # The OpenDark row background measured in FreeCAD.
        self.assertTrue(palette_is_dark(_Palette(_Color(0x29, 0x2c, 0x30))))

    def test_a_light_background_does_not(self) -> None:
        self.assertFalse(palette_is_dark(_Palette(_Color(0xf0, 0xf0, 0xf0))))
        self.assertFalse(palette_is_dark(_Palette(_Color(255, 255, 255))))

    def test_the_threshold_sits_between_the_two(self) -> None:
        self.assertTrue(palette_is_dark(_Palette(_Color(0x7f, 0x7f, 0x7f))))
        self.assertFalse(palette_is_dark(_Palette(_Color(0x80, 0x80, 0x80))))


class _Hex:
    def __init__(self, value):
        self._rgb = tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))

    def redF(self):  # noqa: N802 - Qt API name
        return self._rgb[0]

    def greenF(self):  # noqa: N802 - Qt API name
        return self._rgb[1]

    def blueF(self):  # noqa: N802 - Qt API name
        return self._rgb[2]


class BranchArrowContrastTests(unittest.TestCase):
    """The shipped arrows have to stay legible on the row they sit in."""

    #: The OpenDark row background, measured in FreeCAD.
    ROW = "#292c30"

    def _contrast(self, fill):
        first = relative_luminance(_Hex(fill))
        second = relative_luminance(_Hex(self.ROW))
        lighter, darker = sorted((first, second), reverse=True)
        return (lighter + 0.05) / (darker + 0.05)

    def _fill(self, name):
        path = Path(sync_panel.__file__).resolve().parent.parent / "icons" / name
        source = path.read_text(encoding="utf-8")
        return source.split('fill="', 1)[1].split('"', 1)[0]

    def test_the_shipped_blue_clears_three_to_one(self) -> None:
        for name in ("tree-branch-closed.svg", "tree-branch-open.svg"):
            fill = self._fill(name)
            self.assertGreaterEqual(
                self._contrast(fill), 3.0,
                "%s (%s) is too faint on %s" % (name, fill, self.ROW))

    def test_the_blue_is_blue(self) -> None:
        fill = self._fill("tree-branch-closed.svg")
        red, green, blue = (int(fill[i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreater(blue, red)
        self.assertGreater(blue, green)

    def test_the_exact_theme_accent_would_have_been_too_faint(self) -> None:
        """Documents why the fill is not simply the accent colour."""
        # ThemeAccentColor1 from the OpenDark configuration.
        self.assertLess(self._contrast("#4aa5ff"), 3.0)




if __name__ == "__main__":
    unittest.main()
