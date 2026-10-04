"""FreeCAD document and application lifecycle integration."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Set, Tuple

from .commit_message import (
    CUSTOM_MODE,
    TIMESTAMP_MODE,
    build_commit_message,
    default_message,
)
from .git_service import GitError
from .qt_compat import QtCore, message_icon, show_plain_message

try:  # pragma: no cover - only available inside FreeCAD
    import FreeCAD as App
except ImportError:  # pragma: no cover
    App = None

try:  # pragma: no cover - only available in the FreeCAD GUI
    import FreeCADGui as Gui
except ImportError:  # pragma: no cover
    Gui = None


class _DocumentSaveObserver:
    """Adapter for FreeCAD's addDocumentObserver callback naming."""

    def __init__(self, controller: "SyncController") -> None:
        self.controller = controller

    def slotFinishSaveDocument(self, document, filename) -> None:
        self.controller.document_saved(document, filename)


class SyncController(QtCore.QObject):
    """Serialize automatic save/exit operations and expose lifecycle hooks."""

    def __init__(self, context: Any, panel: Any = None) -> None:
        super().__init__()
        self.context = context
        self.panel = panel
        self._pending_saves: Dict[Tuple[int, int, str], str] = {}
        self._auto_retry_counts: Dict[Tuple[int, int, str], int] = {}
        self._session_saved_paths: Set[str] = set()
        self._bound_repository = None
        self._bound_generation = -1
        self._auto_running = False
        self._observer = None
        self._main_window = None
        self._exit_attempted = False
        self._insecure_exit_confirmed = False
        self._exit_push_declined = False
        self._stopped = False
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self._drain_auto_queue)

        if App is not None and hasattr(App, "addDocumentObserver"):
            try:
                self._observer = _DocumentSaveObserver(self)
                App.addDocumentObserver(self._observer)
            except Exception:
                self._observer = None

        if Gui is not None:
            try:
                self._main_window = Gui.getMainWindow()
                if self._main_window is not None:
                    self._main_window.installEventFilter(self)
            except Exception:
                self._main_window = None
        try:
            application = QtCore.QCoreApplication.instance()
            if application is not None:
                application.aboutToQuit.connect(self._about_to_quit)
        except Exception:
            pass

    def _sync_repository_binding(self) -> None:
        repository = self.context.repository
        generation = int(getattr(self.context, "generation", 0))
        if repository is self._bound_repository and generation == self._bound_generation:
            return
        self._bound_repository = repository
        self._bound_generation = generation
        self._pending_saves.clear()
        self._auto_retry_counts.clear()
        self._session_saved_paths.clear()

    def settings_changed(self) -> None:
        """React immediately when the user edits settings or connection state."""
        self._sync_repository_binding()
        if not self.context.settings.get("auto_commit_save", True):
            self._pending_saves.clear()
            self._auto_retry_counts.clear()
            self._save_timer.stop()

    def _repository(self):
        return self.context.repository

    def document_saved(self, document, filename) -> None:
        if self._stopped:
            return
        self._sync_repository_binding()
        name = str(getattr(document, "Name", ""))
        if name.startswith("GitSyncPreview"):
            return
        path_value = str(filename or getattr(document, "FileName", "") or "").strip()
        if not path_value:
            return
        path = Path(path_value).expanduser()
        repository = self._repository()
        if repository is None:
            return
        try:
            if not repository.contains_path(str(path)):
                return
            absolute = path.resolve()
            relative = repository.relative_path(str(absolute))
        except (OSError, ValueError, GitError):
            return
        generation = int(getattr(self.context, "generation", 0))
        # Remember every in-repository save of this session regardless of the
        # automatic commit setting.  The exit path uses this set to commit only
        # the documents the user actually touched.
        self._session_saved_paths.add(relative)
        if not self.context.settings.get("auto_commit_save", True):
            return
        key = (id(repository), generation, str(absolute))
        self._pending_saves[key] = relative
        self._save_timer.start()

    def _auto_message(self, relative_paths) -> str:
        """Build the automatic save commit message from the settings."""
        return build_commit_message(
            self.context.settings, relative_paths, prefix="Auto-save"
        )

    def _commit_message_is_prompted(self) -> bool:
        """True when the user asked to type the message on every save."""
        mode = str(self.context.settings.get("auto_commit_message", TIMESTAMP_MODE) or "")
        return mode.lower() == CUSTOM_MODE

    def _drain_auto_queue(self) -> None:
        if self._stopped or self._auto_running:
            return
        self._sync_repository_binding()
        if not self.context.settings.get("auto_commit_save", True):
            self._pending_saves.clear()
            self._auto_retry_counts.clear()
            return
        repository = self._repository()
        if repository is None:
            self._pending_saves.clear()
            self._auto_retry_counts.clear()
            return

        generation = int(getattr(self.context, "generation", 0))
        records = []
        for key, relative in list(self._pending_saves.items()):
            if key[0] == id(repository) and key[1] == generation:
                records.append((key, relative))
            else:
                self._pending_saves.pop(key, None)
                self._auto_retry_counts.pop(key, None)
        if not records:
            return

        records.sort(key=lambda item: item[1])
        keys = [item[0] for item in records]
        paths = [item[1] for item in records]
        for key in keys:
            self._pending_saves.pop(key, None)
            self._auto_retry_counts[key] = self._auto_retry_counts.get(key, 0) + 1

        message = self._auto_message(paths)
        if self._commit_message_is_prompted():
            # The user asked to write the message themselves.  Ask on the GUI
            # thread before spending time on preview rendering.  A panel that
            # cannot prompt (headless use) falls back to the default message.
            ask = getattr(self.panel, "ask_commit_message", None)
            if callable(ask):
                answer = ask(paths)
                if answer is None:
                    # Cancelled: the document stays as an uncommitted change
                    # and the exit commit can still pick it up.
                    for key in keys:
                        self._auto_retry_counts.pop(key, None)
                    if hasattr(self.panel, "commit_message_cancelled"):
                        self.panel.commit_message_cancelled(paths)
                    return
                text = str(answer).strip()
                if text:
                    message = text
                    remember = getattr(self.panel, "remember_commit_message", None)
                    if callable(remember):
                        remember(text)
                else:
                    message = default_message("Auto-save", paths)
        # A commit is a purely local operation, so only the optional automatic
        # push needs the unencrypted-transport confirmation.  Declining the
        # push still commits the document and its preview locally.
        auto_push = bool(self.context.settings.get("auto_push_save", False))
        if auto_push and self.panel is not None and hasattr(self.panel, "confirm_insecure_operation"):
            if not self.panel.confirm_insecure_operation("Automatic push"):
                auto_push = False
        preview_paths = []
        if self.panel is not None and hasattr(self.panel, "prepare_commit_previews"):
            if hasattr(self.panel, "auto_sync_preparing"):
                self.panel.auto_sync_preparing()
            try:
                preview_result = self.panel.prepare_commit_previews(paths)
            except BaseException as exc:
                for key, relative in zip(keys, paths):
                    if self._auto_retry_counts.get(key, 0) <= 1:
                        self._pending_saves[key] = relative
                self._auto_retry_counts.clear()
                if self.panel is not None:
                    self.panel.auto_sync_failed(exc)
                return
            preview_paths = list(preview_result.get("paths", []))
        commit_paths = sorted(set(paths) | set(preview_paths))

        self._auto_running = True

        def is_current() -> bool:
            return (
                not self._stopped
                and self.context.repository is repository
                and int(getattr(self.context, "generation", 0)) == generation
            )

        def operation():
            if not is_current():
                return {"stale": True}
            result = repository.commit_paths(commit_paths, message)
            if not is_current():
                return {"stale": True, "commit": result}
            # Pushing is a plain ``git push``: previews were already regenerated
            # on the GUI thread and committed together with the model files.
            # ``git push`` reports on stderr, so the exit status is the signal.
            pushed = False
            if result.get("created") and auto_push:
                pushed = repository.push().ok
            return {"commit": result, "push": pushed}

        def success(result: Dict[str, Any]) -> None:
            self._auto_running = False
            if self._stopped:
                return
            if not is_current():
                self._drain_auto_queue()
                return
            if not result.get("stale"):
                for key in keys:
                    self._auto_retry_counts.pop(key, None)
                # The exit pass only needs to cover documents the automatic
                # commit did not already handle.
                for committed in commit_paths:
                    self._session_saved_paths.discard(committed)
                if self.panel is not None:
                    self.panel.auto_sync_finished(result)
            self._drain_auto_queue()

        def failure(error: BaseException) -> None:
            self._auto_running = False
            if self._stopped or not is_current():
                return
            # Preserve a single bounded retry for transient index locks or
            # temporary network failures.  Persistent authentication errors
            # are left for the user to fix and retry explicitly.
            retry = False
            for key, relative in zip(keys, paths):
                if self._auto_retry_counts.get(key, 0) <= 1:
                    self._pending_saves[key] = relative
                    retry = True
            if self.panel is not None:
                self.panel.auto_sync_failed(error)
            if retry:
                QtCore.QTimer.singleShot(5000, self._drain_auto_queue)
            else:
                self._drain_auto_queue()

        self.context.runner.submit(
            operation,
            on_success=success,
            on_error=failure,
        )

    def forget_session_paths(self, paths=None) -> None:
        """Drop paths a manual commit already handled, so exit can skip them.

        ``None`` means a commit of everything, so no session document is left.
        """
        if paths is None:
            self._session_saved_paths.clear()
            return
        for path in paths:
            self._session_saved_paths.discard(str(path))

    def _show_exit_warning(self, error: BaseException) -> None:
        message = str(error)
        token = str(self.context.settings.get("token", ""))
        if token:
            message = message.replace(token, "***")
        if Gui is not None:
            try:
                show_plain_message(
                    Gui.getMainWindow(),
                    message_icon("Warning"),
                    "GitSync: push pending commits",
                    "GitSync could not push pending commits before FreeCAD exits.\n\n"
                    "Your local commits are still available in the workspace.\n\n{}".format(message),
                )
                return
            except Exception:
                pass
        print("GitSync: push pending commits failed: {}".format(message))

    def _open_repository_documents(self) -> Set[str]:
        """Return repository documents that are still open and modified."""
        repository = self._repository()
        if repository is None or App is None:
            return set()
        paths: Set[str] = set()
        try:
            documents = list(App.listDocuments().values())
        except Exception:
            return paths
        for document in documents:
            name = str(getattr(document, "Name", ""))
            if name.startswith("GitSyncPreview"):
                continue
            file_name = str(getattr(document, "FileName", "") or "").strip()
            if not file_name:
                continue
            try:
                modified = bool(
                    getattr(document, "Modified", False)
                    or (getattr(document, "isTouched", None) or (lambda: False))()
                )
            except Exception:
                modified = False
            if not modified:
                continue
            path = Path(file_name).expanduser()
            try:
                if not repository.contains_path(str(path)):
                    continue
                paths.add(repository.relative_path(str(path.resolve())))
            except (OSError, ValueError, GitError):
                continue
        return paths

    def _exit_commit_paths(self) -> Set[str]:
        """Documents to commit on exit: session saves plus modified open files."""
        return set(self._session_saved_paths) | self._open_repository_documents()

    def _confirm_exit_push(self) -> bool:
        """Resolve unencrypted-HTTP consent while the main window still exists."""
        if self._exit_push_declined:
            return False
        if self._insecure_exit_confirmed:
            return True
        if self.panel is None or not hasattr(self.panel, "confirm_insecure_operation"):
            return True
        if not self.panel.confirm_insecure_operation("Exit push"):
            self._exit_push_declined = True
            return False
        self._insecure_exit_confirmed = True
        return True

    def push_on_exit(self, force: bool = False, final: bool = False) -> None:
        """Two-phase exit handling.

        ``final=False`` runs from the main-window Close event and only collects
        the user's consent, because modal dialogs need a visible window.
        ``final=True`` runs from ``aboutToQuit`` once FreeCAD has saved and
        closed its documents; it commits exactly the documents touched in this
        session (together with their regenerated previews) and then performs a
        plain ``git push``.  The repository is never scanned model by model.
        """
        if self._stopped:
            return
        if not self.context.settings.get("push_on_exit", False):
            return
        if not final:
            self._confirm_exit_push()
            return
        if self._exit_attempted and not force:
            return
        self._exit_attempted = True
        repository = self._repository()
        if repository is None:
            return
        try:
            if not repository.is_repository():
                return
            # Finish an already queued save operation before touching the
            # working tree synchronously.  Never start a second Git process
            # against the same working tree if the background task still runs.
            auto_commit = bool(self.context.settings.get("auto_commit_save", True))
            if self._pending_saves and auto_commit:
                self._drain_auto_queue()
            if not self.context.runner.wait_for_idle(5000):
                raise GitError("A background GitSync operation is still running; push was skipped.")
            if self._pending_saves and auto_commit:
                self._drain_auto_queue()
                if not self.context.runner.wait_for_idle(5000):
                    raise GitError("A background GitSync operation is still running; push was skipped.")
            commit_paths = self._exit_commit_paths()
            if commit_paths and self.panel is not None and hasattr(self.panel, "prepare_exit_commit"):
                # A preview failure must not block the push of the commits that
                # already exist, so it is reported and the push continues.
                try:
                    self.panel.prepare_exit_commit(sorted(commit_paths))
                except BaseException as exc:
                    print("GitSync: exit commit failed: {}".format(exc))
            self._session_saved_paths.clear()
            if not self._confirm_exit_push():
                return
            snapshot = repository.status()
            if snapshot.get("upstream") and int(snapshot.get("ahead", 0) or 0) == 0:
                return
            repository.push(timeout=30)
        except BaseException as exc:
            self._show_exit_warning(exc)

    def _about_to_quit(self) -> None:
        # Main-window close events ask for consent and free the UI.  The final
        # pass runs here, after FreeCAD has closed and saved its documents, so
        # only the files the user actually opened/saved in this session are
        # committed and pushed.
        if self.context.settings.get("push_on_exit", False):
            self.push_on_exit(force=True, final=True)
        self.shutdown()

    def eventFilter(self, obj, event) -> bool:
        close_event = getattr(QtCore.QEvent, "Close", None)
        if close_event is None:
            close_event = getattr(QtCore.QEvent.Type, "Close", None)
        if self._main_window is not None and event.type() == close_event:
            # Only ask for consent here: the commit/push itself has to wait
            # until aboutToQuit, after FreeCAD saved and closed its documents.
            self.push_on_exit(final=False)
            # If FreeCAD cancels the close because a document needs attention,
            # allow a later close attempt to ask again.
            QtCore.QTimer.singleShot(0, self._reset_exit_attempt_if_open)
        return False

    def _reset_exit_attempt_if_open(self) -> None:
        if self._stopped or self._main_window is None:
            return
        try:
            if self._main_window.isVisible():
                self._exit_attempted = False
                self._insecure_exit_confirmed = False
                self._exit_push_declined = False
        except Exception:
            pass

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._save_timer.stop()
        if hasattr(self.context.runner, "shutdown"):
            self.context.runner.shutdown(5000)
        if App is not None and self._observer is not None and hasattr(App, "removeDocumentObserver"):
            try:
                App.removeDocumentObserver(self._observer)
            except Exception:
                pass
        if self._main_window is not None:
            try:
                self._main_window.removeEventFilter(self)
            except Exception:
                pass
        self._observer = None
        self._main_window = None


__all__ = ["SyncController"]
