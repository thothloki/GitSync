"""The dockable GitSync panel."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import os

from .commit_message import build_commit_message
from .commit_message import timestamp as commit_timestamp
from .dialogs import SettingsDialog, SetupDialog, StartupSyncDialog
from .git_service import (
    GitError,
    GitRepository,
    canonical_remote_target,
    normalized_model_filename,
)
from .model_list import (
    MODIFIED_SORT,
    NAME_SORT,
    filter_models,
    format_timestamp,
    group_models,
    newest_time,
    normalize_sort_order,
)
from .model_preview import ModelPreview, is_supported_model
from .os_integration import reveal_in_file_manager
from .qt_compat import (
    QtCore,
    QtWidgets,
    align_center,
    custom_context_menu_policy,
    dialog_accepted,
    exec_dialog,
    header_fixed_mode,
    header_stretch_mode,
    keep_aspect_ratio,
    line_edit_normal_mode,
    message_buttons,
    message_icon,
    message_no,
    message_yes,
    palette_is_dark,
    set_plain_text,
    show_plain_message,
    show_trust_prompt,
    smooth_transformation,
    user_role,
    wait_cursor,
)
from .workspace_files import (
    WorkspaceFileError,
    removal_paths,
    rename_pairs,
    rename_target,
)

try:  # pragma: no cover - only available inside FreeCAD
    import FreeCAD as App
except ImportError:  # pragma: no cover
    App = None

try:  # pragma: no cover - only available in the FreeCAD GUI
    import FreeCADGui as Gui
except ImportError:  # pragma: no cover
    Gui = None


#: Directory holding the workbench's own artwork, next to this package.
_ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")


def _workbench_icon(name: str) -> str:
    """Return the path of a shipped icon, or "" when it is missing."""
    candidate = os.path.join(_ICONS_DIR, name)
    return candidate if os.path.isfile(candidate) else ""


def _qt_url(path: str) -> str:
    """Escape a filesystem path for use in a Qt stylesheet ``url()``.

    Stylesheet parsing stops at the first ``)``, so a path containing one has
    to be escaped.
    """
    return path.replace("\\", "\\\\").replace(")", "\\)")


class SyncPanel(QtWidgets.QWidget):
    """Repository controls, model browser, and compact static preview."""

    def __init__(self, context: Any) -> None:
        super().__init__()
        self.context = context
        self.context.panel = self
        self._startup_checked = False
        self._preview_warning_shown = False
        self._select_after_refresh = None
        self._locally_created_paths = set()
        self._startup_entries: List[Dict[str, Any]] = []
        self._model_paths: List[str] = []
        self._model_mtimes: Dict[str, float] = {}
        self._model_filter = ""
        self._model_items = {}
        self._preview = ModelPreview(bool(context.settings.get("preview_home", True)))
        self._build_ui()
        self.context.runner.busy_changed.connect(self._busy_changed)
        QtCore.QTimer.singleShot(0, self._initial_load)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setObjectName("GitSyncPanel")
        self.setMinimumWidth(330)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        connection_group = QtWidgets.QGroupBox("Repository")
        connection_layout = QtWidgets.QVBoxLayout(connection_group)
        self.connection_label = QtWidgets.QLabel("No repository configured")
        self.connection_label.setWordWrap(True)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)
        connection_layout.addWidget(self.connection_label)
        connection_layout.addWidget(self.status_label)

        connection_buttons = QtWidgets.QHBoxLayout()
        connection_buttons.setSpacing(6)
        # No separate Connect / Clone button: the Settings dialog carries the
        # same repository fields, so a second way to reach them would only
        # duplicate the connection form.
        self.settings_button = QtWidgets.QPushButton("Settings")
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        connection_buttons.addWidget(self.settings_button)
        connection_buttons.addWidget(self.refresh_button)
        connection_layout.addLayout(connection_buttons)
        root.addWidget(connection_group)

        sync_group = QtWidgets.QGroupBox("Synchronization")
        sync_layout = QtWidgets.QVBoxLayout(sync_group)
        sync_buttons = QtWidgets.QHBoxLayout()
        self.pull_button = QtWidgets.QPushButton("Pull")
        self.push_button = QtWidgets.QPushButton("Push")
        sync_buttons.addWidget(self.pull_button)
        sync_buttons.addWidget(self.push_button)
        sync_layout.addLayout(sync_buttons)
        root.addWidget(sync_group)

        browser_group = QtWidgets.QGroupBox("Repository models")
        browser_layout = QtWidgets.QVBoxLayout(browser_group)
        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setPlaceholderText("Search models by name…")
        self.search_edit.setToolTip(
            "Filter the list to models whose file name or folder contains the text."
        )
        self.search_edit.setClearButtonEnabled(True)
        browser_layout.addWidget(self.search_edit)
        self.sort_combo = QtWidgets.QComboBox()
        self.sort_combo.addItem("Sort: name (A-Z)", NAME_SORT)
        self.sort_combo.addItem("Sort: date modified (newest first)", MODIFIED_SORT)
        self.sort_combo.setToolTip(
            "Order the model list by file name or by the time a model was last edited."
        )
        saved_order = normalize_sort_order(
            self.context.settings.get("model_sort", NAME_SORT)
        )
        self.sort_combo.setCurrentIndex(1 if saved_order == MODIFIED_SORT else 0)
        browser_layout.addWidget(self.sort_combo)
        self.models_tree = QtWidgets.QTreeWidget()
        self.models_tree.setHeaderLabels(["Model", "Modified"])
        self.models_tree.setMinimumHeight(150)
        # GitSync builds its own menu, so the default one is turned off.
        self.models_tree.setContextMenuPolicy(custom_context_menu_policy())
        self.models_tree.header().setSectionResizeMode(0, header_stretch_mode())
        # The date column carries no more than "YYYY-MM-DD", so pin it to just
        # enough width for that and let the model name take everything else.
        self._size_modified_column()
        self._apply_branch_indicator()
        browser_layout.addWidget(self.models_tree)

        self.preview_path_label = QtWidgets.QLabel("Select a model to preview")
        self.preview_path_label.setWordWrap(True)
        browser_layout.addWidget(self.preview_path_label)
        model_row = QtWidgets.QHBoxLayout()
        self.new_model_button = QtWidgets.QPushButton("New Model")
        self.open_model_button = QtWidgets.QPushButton("Open Model")
        self.open_model_button.setEnabled(False)
        model_row.addWidget(self.new_model_button)
        model_row.addWidget(self.open_model_button)
        browser_layout.addLayout(model_row)

        self.preview_label = QtWidgets.QLabel("No model selected")
        self.preview_label.setAlignment(align_center())
        self.preview_label.setWordWrap(True)
        self.preview_label.setMinimumSize(260, 170)
        self.preview_label.setStyleSheet("QLabel { background: #303030; color: #dddddd; padding: 4px; }")
        browser_layout.addWidget(self.preview_label)
        root.addWidget(browser_group, 1)

        self.settings_button.clicked.connect(self.open_settings)
        self.refresh_button.clicked.connect(lambda _checked=False: self.refresh_and_check())
        self.pull_button.clicked.connect(self.pull)
        self.push_button.clicked.connect(self.push)
        self.open_model_button.clicked.connect(self.open_selected_model)
        self.new_model_button.clicked.connect(self.new_model)
        self.models_tree.itemSelectionChanged.connect(self._selection_changed)
        self.models_tree.customContextMenuRequested.connect(self._model_context_menu)
        self.models_tree.itemDoubleClicked.connect(lambda _item, _column: self.open_selected_model())
        self.sort_combo.currentIndexChanged.connect(self._sort_order_changed)
        self.search_edit.textChanged.connect(self._model_filter_changed)

    def _initial_load(self) -> None:
        repository = self._repository()
        if repository is None or not repository.workspace.exists():
            self._update_no_repository()
            return
        if not repository.workspace.is_dir():
            self._update_no_repository()
            self._set_status("The configured workspace is not a directory.")
            return
        if not repository.is_repository():
            set_plain_text(self.connection_label, "Workspace ready for cloning")
            self._set_status("Use Settings to choose a remote and initialize this directory.")
            self._set_operations_enabled(False)
            return
        self.refresh(self._startup_check_after_refresh, warn_insecure=False)

    # ------------------------------------------------------------------
    # Common UI helpers
    # ------------------------------------------------------------------
    def _update_no_repository(self) -> None:
        set_plain_text(self.connection_label, "No repository configured")
        set_plain_text(self.status_label, "Use Settings to connect to a remote repository.")
        self.models_tree.clear()
        self._model_items = {}
        self._model_paths = []
        self._model_mtimes = {}
        set_plain_text(self.preview_path_label, "Select a model to preview")
        set_plain_text(self.preview_label, "No model selected")
        self.open_model_button.setEnabled(False)
        self._set_operations_enabled(False)

    def _busy_changed(self, busy: bool) -> None:
        self._set_operations_enabled(not busy and self.context.repository is not None)
        if busy:
            set_plain_text(self.status_label, "Git operation in progress…")

    def _set_operations_enabled(self, enabled: bool) -> None:
        for button in (
            self.refresh_button,
            self.pull_button,
            self.push_button,
            self.new_model_button,
            self.models_tree,
        ):
            button.setEnabled(enabled)
        self.settings_button.setEnabled(not self.context.runner.busy)

    def _set_status(self, text: str) -> None:
        set_plain_text(self.status_label, text)

    def _insecure_http_enabled(self) -> bool:
        return str(self.context.settings.get("server_url", "")).lower().startswith("http://")

    def _confirm_insecure_operation(self, operation: str) -> bool:
        if not self._insecure_http_enabled() or self.context.settings.get("allow_insecure_http", False):
            return True
        answer = show_plain_message(
            self,
            message_icon("Warning"),
            "Unencrypted HTTP connection",
            "{} will use plain HTTP. Credentials and repository data will not be encrypted in transit.\n\n"
            "Continue?".format(operation),
            message_buttons(message_yes(), message_no()),
            message_no(),
        )
        return answer == message_yes()

    def _show_error(self, error: BaseException) -> None:
        text = str(error)
        token = str(self.context.settings.get("token", ""))
        if token:
            text = text.replace(token, "***")
        self._set_status("Error: {}".format(text))
        show_plain_message(
            self,
            message_icon("Critical"),
            "GitSync",
            text,
        )

    def _repository(self) -> Optional[GitRepository]:
        return self.context.repository

    def _submit(self, function, *args, on_success=None, on_error=None, **kwargs) -> None:
        self.context.runner.submit(
            function,
            *args,
            on_success=on_success,
            on_error=on_error,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Connection and settings
    # ------------------------------------------------------------------
    def open_setup(self) -> None:
        if self.context.runner.busy:
            return
        dialog = SetupDialog(
            dict(self.context.settings),
            self,
            allow_insecure=bool(self.context.settings.get("allow_insecure_http", False)),
        )
        if exec_dialog(dialog) != dialog_accepted():
            return
        values = dict(self.context.settings)
        values.update(dialog.values())
        candidate = GitRepository(
            workspace=values["workspace"],
            remote_url=values["server_url"],
            username=values["username"],
            token=values["token"],
        )
        self._connect_candidate(candidate, values)

    def open_settings(self) -> None:
        if self.context.runner.busy:
            return
        dialog = SettingsDialog(dict(self.context.settings), self.context.settings_store, self)
        if exec_dialog(dialog) != dialog_accepted():
            return
        try:
            values = dialog.values()
        except Exception as error:
            # Never let a broken value lookup close the dialog silently: the
            # user would assume their new settings had been applied.
            self._show_error(error)
            return
        old = dict(self.context.settings)
        values = dialog.values()
        connection_changed = (
            canonical_remote_target(old.get("server_url", ""), old.get("username", ""))
            != canonical_remote_target(values.get("server_url", ""), values.get("username", ""))
            or old.get("workspace") != values.get("workspace")
        )
        self._preview.apply_home = bool(values.get("preview_home", True))
        self._startup_checked = False
        if not values.get("server_url") and not values.get("workspace") and (
            old.get("server_url") or old.get("workspace")
        ):
            try:
                self.context.save_settings(values)
            except Exception as error:
                self._show_error(error)
                return
            self._update_no_repository()
            return
        if connection_changed:
            candidate = GitRepository(
                workspace=values["workspace"],
                remote_url=values["server_url"],
                username=values["username"],
                token=values["token"],
            )
            self._connect_candidate(candidate, values)
        else:
            try:
                self.context.save_settings(values)
            except Exception as error:
                self._show_error(error)
                return
            self.refresh(self._startup_check_after_refresh, warn_insecure=False)

    def _connect_candidate(self, repository: GitRepository, values: Dict[str, Any]) -> None:
        """Connect a candidate repository without replacing active state early."""
        self._set_status("Connecting to remote repository…")
        self.settings_button.setEnabled(False)

        def success(_result: Any) -> None:
            # Persist credentials/settings only after clone or remote
            # validation succeeded.  A failed attempt leaves the previous
            # repository, token, and workspace untouched.
            try:
                self.context.save_settings(values, repository=repository)
            except BaseException as error:
                failure(error)
                return
            self._preview_warning_shown = False
            self._locally_created_paths.clear()
            self._startup_checked = False
            self._set_status("Connected. Checking repository status…")
            self.settings_button.setEnabled(True)
            self.refresh(self._startup_check_after_refresh, warn_insecure=False)

        def failure(error: BaseException) -> None:
            self.settings_button.setEnabled(True)
            self._show_error(error)

        self._submit(
            repository.connect_or_clone,
            repository.remote_url,
            on_success=success,
            on_error=failure,
        )

    # ------------------------------------------------------------------
    # Refresh/status/model browser
    # ------------------------------------------------------------------
    def refresh_and_check(self, warn_insecure: bool = True) -> None:
        """Refresh, then run the same comparison FreeCAD makes when it starts.

        This is what the **Refresh** button and command call: fetch the remote,
        compare it with the local branch, and raise the startup dialog when
        there is something to decide about.  The check is otherwise one-shot per
        session, so it is re-armed here — a deliberate Refresh is a fresh start.
        """
        self._startup_checked = False
        self.refresh(self._startup_check_after_refresh, warn_insecure=warn_insecure)

    def refresh(self, on_success=None, warn_insecure: bool = True) -> None:
        if self.context.runner.busy:
            return
        if warn_insecure and not self._confirm_insecure_operation("Refresh"):
            return
        repository = self._repository()
        if repository is None:
            self._update_no_repository()
            return
        if on_success is None:
            on_success = self._apply_snapshot
        self._set_status("Refreshing repository status…")
        self._submit(repository.snapshot, on_success=on_success, on_error=self._show_error)

    def _apply_snapshot(self, snapshot: Dict[str, Any]) -> None:
        branch = snapshot.get("branch", "unknown")
        upstream = snapshot.get("upstream") or "no upstream"
        ahead = int(snapshot.get("ahead", 0) or 0)
        behind = int(snapshot.get("behind", 0) or 0)
        remote = snapshot.get("remote", "")
        if snapshot.get("root"):
            self._preview.allowed_root = str(snapshot["root"])
        set_plain_text(
            self.connection_label,
            "{}  •  {}\n{}".format(branch, upstream, remote or "No origin remote"),
        )
        if snapshot.get("entries"):
            self._set_status("{} uncommitted change(s); {} ahead / {} behind".format(
                len(snapshot["entries"]), ahead, behind
            ))
        else:
            self._set_status("{} ahead / {} behind • {}".format(
                ahead, behind, snapshot.get("last_commit", "No commits yet")
            ))
        self._model_paths = list(snapshot.get("models", []))
        self._model_mtimes = dict(snapshot.get("model_mtimes") or {})
        self._populate_models(self._model_paths, self._model_mtimes)
        self._set_operations_enabled(True)

    def _size_modified_column(self) -> None:
        """Pin the Modified column to the width of a date and no more.

        Measured from the widget's own font rather than hard-coded, so the
        column stays snug under other font sizes and DPI settings while the
        model name column keeps the remaining width.
        """
        header = self.models_tree.header()
        # stretchLastSection defaults to True and would override the Fixed mode
        # below, giving the last column half the width instead of a snug one.
        header.setStretchLastSection(False)
        header.setSectionResizeMode(1, header_fixed_mode())
        metrics = self.models_tree.fontMetrics()
        widest = max(metrics.horizontalAdvance(text) for text in ("2026-01-31", "Modified"))
        header.resizeSection(1, widest + 2 * metrics.horizontalAdvance(" ") + 12)

    def _apply_branch_indicator(self) -> None:
        """Give the folder arrows readable contrast on a dark theme.

        A theme's ``QTreeView::branch`` image is usually a large SVG scaled
        into the tree's small branch area, where antialiasing leaves only the
        glyph's fringe: measured at 1.5:1 against the row background, i.e.
        dimmer than the model text beside it.  Qt draws no arrow of its own
        when a stylesheet supplies an image, so clearing the image is not an
        option.  Under a dark palette GitSync therefore supplies its own
        small, solid arrows; a light theme keeps whatever it already draws.
        """
        if not palette_is_dark(self.models_tree.palette()):
            # Cleared rather than skipped, so calling this again after a
            # palette change hands the tree back to the theme.
            self.models_tree.setStyleSheet("")
            return
        closed = _workbench_icon("tree-branch-closed.svg")
        opened = _workbench_icon("tree-branch-open.svg")
        if not closed or not opened:
            self.models_tree.setStyleSheet("")
            return
        # Both "has siblings" and "last child" states get the same arrow; the
        # tree has no siblings-of-last-child distinction to preserve.
        self.models_tree.setStyleSheet(
            "QTreeView::branch:closed:has-children:has-siblings,"
            "QTreeView::branch:has-children:!has-siblings:closed"
            " {{ image: url({}); }}"
            "QTreeView::branch:open:has-children:has-siblings,"
            "QTreeView::branch:open:has-children:!has-siblings"
            " {{ image: url({}); }}".format(_qt_url(closed), _qt_url(opened))
        )

    def _sort_order(self) -> str:
        data = self.sort_combo.currentData() if hasattr(self, "sort_combo") else None
        return normalize_sort_order(data or NAME_SORT)

    def _sort_order_changed(self) -> None:
        """Persist the chosen order and reorder the list right away."""
        values = dict(self.context.settings)
        values["model_sort"] = self._sort_order()
        try:
            self.context.settings = self.context.settings_store.save(values)
        except Exception as error:
            self._show_error(error)
            return
        # Reorder with the data already at hand so the new order shows up even
        # while a Git operation is still running.
        if self._model_paths:
            self._populate_models(self._model_paths, self._model_mtimes)
        if self.context.runner.busy:
            return
        self.refresh(warn_insecure=False)

    def _model_filter_changed(self, _text: str = "") -> None:
        """Re-filter the already loaded list; no Git call is needed."""
        self._model_filter = str(self.search_edit.text() or "").strip()
        if not self._model_paths:
            return
        self._populate_models(self._model_paths, self._model_mtimes)

    def _populate_models(self, models: Iterable[str], mtimes=None) -> None:
        selected = self._select_after_refresh or self._selected_model_path()
        self._select_after_refresh = None
        visible = filter_models(models, self._model_filter)
        self.models_tree.blockSignals(True)
        self.models_tree.clear()
        self._model_items = {}
        folders: Dict[str, QtWidgets.QTreeWidgetItem] = {}
        times = dict(mtimes or {})

        def folder_item(
            parts, path: str = "", newest: str = ""
        ) -> Optional[QtWidgets.QTreeWidgetItem]:
            parent: Optional[QtWidgets.QTreeWidgetItem] = None
            prefix = ""
            for folder in parts:
                prefix = str(Path(prefix) / folder) if prefix else folder
                if prefix not in folders:
                    # A folder is ordered by the newest file inside it, so the
                    # date it sorts by is shown on the row.  Only the node the
                    # models really live in gets it, not an intermediate one.
                    item = QtWidgets.QTreeWidgetItem(
                        [folder, newest if prefix == path else ""]
                    )
                    if parent is None:
                        self.models_tree.addTopLevelItem(item)
                    else:
                        parent.addChild(item)
                    folders[prefix] = item
                parent = folders[prefix]
            return parent

        for folder, files in group_models(visible, times, self._sort_order()):
            newest = format_timestamp(newest_time(files, times))
            parent = folder_item(
                Path(folder).parts if folder else (), folder, newest
            )
            for relative in files:
                file_item = QtWidgets.QTreeWidgetItem(
                    [Path(relative).name, format_timestamp(times.get(relative))]
                )
                file_item.setData(0, user_role(), relative)
                file_item.setToolTip(0, relative)
                if parent is None:
                    self.models_tree.addTopLevelItem(file_item)
                else:
                    parent.addChild(file_item)
                self._model_items[relative] = file_item
        # Folders start collapsed so a long list stays scannable; a restored
        # selection has its folders opened below, otherwise it would be hidden.
        self.models_tree.collapseAll()
        restored = selected if selected and selected in self._model_items else None
        if restored:
            item = self._model_items[restored]
            self._reveal_item(item)
            self.models_tree.setCurrentItem(item)
        self.models_tree.blockSignals(False)
        self.open_model_button.setEnabled(bool(restored))
        if restored:
            self._render_preview(restored)
        elif not visible and self._model_filter:
            set_plain_text(self.preview_path_label, "Select a model to preview")
            set_plain_text(
                self.preview_label,
                "No model matches '{}'".format(self._model_filter),
            )
        else:
            set_plain_text(self.preview_path_label, "Select a model to preview")
            set_plain_text(self.preview_label, "No model selected")

    def _reveal_item(self, item) -> None:
        """Open the folders above a row so a restored selection stays visible.

        Folders are collapsed by default, so restoring a model that lives in
        one would otherwise leave the selection — and the preview that follows
        it — off screen behind a closed folder.
        """
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()

    def _selected_model_path(self) -> Optional[str]:
        item = self.models_tree.currentItem()
        if item is None:
            return None
        value = item.data(0, user_role())
        return str(value) if value else None

    def _startup_check_after_refresh(self, snapshot: Dict[str, Any]) -> None:
        if self._startup_checked or self._repository() is None:
            return
        if not self._confirm_insecure_operation("Remote refresh"):
            # Shared by the start-up check and the Refresh button, so the wording
            # must not claim it was a start-up.
            self._set_status(
                "Refresh of the remote was skipped because the connection is unencrypted."
            )
            return
        self._startup_checked = True
        repository = self._repository()
        if repository is None:
            return

        def operation():
            fetch_result = repository.fetch()
            current = repository.snapshot()
            current["fetch_output"] = fetch_result.output
            upstream = current.get("upstream")
            if upstream:
                if int(current.get("ahead", 0) or 0):
                    current["local_commits"] = repository.log_range("{}..HEAD".format(upstream))
                    current["local_files"] = repository.changed_files("{}..HEAD".format(upstream))
                if int(current.get("behind", 0) or 0):
                    current["remote_commits"] = repository.log_range("HEAD..{}".format(upstream))
                    current["remote_files"] = repository.changed_files("HEAD..{}".format(upstream))
            return current

        def success(snapshot: Dict[str, Any]) -> None:
            self._apply_snapshot(snapshot)
            if (
                snapshot.get("entries")
                or int(snapshot.get("ahead", 0) or 0)
                or int(snapshot.get("behind", 0) or 0)
            ):
                self._startup_entries = list(snapshot.get("entries", []))
                choice = StartupSyncDialog.show_dialog(snapshot, self)
                self._perform_startup_choice(choice)
            else:
                self._set_status("Local and remote repositories are synchronized.")

        def failure(error: BaseException) -> None:
            error_text = str(error)
            token = str(self.context.settings.get("token", ""))
            if token:
                error_text = error_text.replace(token, "***")
            self._set_status("Repository check failed: {}".format(error_text))
            choice = StartupSyncDialog.show_dialog({}, self, error_text)
            if choice == "retry":
                self._startup_checked = False
                self._startup_check_after_refresh({})

        self._submit(operation, on_success=success, on_error=failure)

    def _perform_startup_choice(self, choice: str) -> None:
        if choice == "push":
            self.push()
        elif choice == "pull":
            self.pull()
        elif choice == "merge":
            self._run_startup_history_operation("merge")
        elif choice == "rebase":
            self._run_startup_history_operation("rebase")
        elif choice == "commit":
            self.commit_working_tree(self._startup_entries)
        elif choice == "upstream":
            repository = self._repository()
            if repository is not None:
                self._set_status("Setting the branch upstream…")
                self._submit(
                    repository.set_upstream,
                    on_success=lambda _result: self.refresh(warn_insecure=False),
                    on_error=self._show_error,
                )
        elif choice == "review":
            self.window().raise_()
            self.window().activateWindow()

    def _run_startup_history_operation(self, operation_name: str) -> None:
        repository = self._repository()
        if repository is None:
            return
        function = repository.merge_remote if operation_name == "merge" else repository.rebase_remote
        self._set_status("{} remote changes…".format(operation_name.capitalize()))
        self._submit(
            function,
            on_success=lambda _result: self.refresh(warn_insecure=False),
            on_error=self._show_error,
        )

    def prepare_commit_previews(self, relative_paths) -> Dict[str, Any]:
        """Regenerate PNGs only for model paths participating in a commit."""
        repository = self._repository()
        if repository is None:
            return {"paths": [], "errors": []}
        root = repository.root.resolve()
        candidates = sorted({str(path) for path in relative_paths if path})
        model_paths = []
        preview_paths = []
        errors = []
        for relative in candidates:
            if not is_supported_model(relative):
                continue
            model_path = root / relative
            if model_path.is_file():
                model_paths.append(relative)
                continue
            try:
                if not repository.contains_path(str(model_path)):
                    continue
                png_path = model_path.with_suffix(".png")
                if png_path.is_symlink():
                    raise ValueError("symbolic-link preview target")
                png_path.resolve(strict=False).relative_to(root)
                if png_path.is_file():
                    preview_paths.append(png_path.relative_to(root).as_posix())
            except (OSError, ValueError) as exc:
                errors.append({"path": relative, "error": str(exc)})

        if model_paths:
            QtWidgets.QApplication.setOverrideCursor(wait_cursor())
            try:
                def progress(index: int, total: int, relative: str) -> None:
                    self._set_status("Generating preview {}/{}: {}".format(index, total, relative))

                result = self._preview.refresh_pngs(
                    model_paths,
                    allowed_root=str(root),
                    force=True,
                    progress=progress,
                )
                for target in result.get("written", []):
                    try:
                        preview_paths.append(Path(target).resolve().relative_to(root).as_posix())
                    except (OSError, ValueError):
                        pass
                errors.extend(result.get("errors", []))
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()
        return {"paths": sorted(set(preview_paths)), "errors": errors}

    def confirm_insecure_operation(self, operation: str) -> bool:
        return self._confirm_insecure_operation(operation)

    # ------------------------------------------------------------------
    # Automatic commit message
    # ------------------------------------------------------------------
    def _ask_text(self, title: str, label: str, initial: str) -> Optional[str]:
        """Show a single-line prompt; ``None`` means the user cancelled."""
        text, accepted = QtWidgets.QInputDialog.getText(
            self, title, label, line_edit_normal_mode(), initial
        )
        if not accepted:
            return None
        return str(text)

    def ask_commit_message(self, relative_paths) -> Optional[str]:
        """Ask the user for the commit message of an automatic save.

        Returns ``None`` when the prompt was cancelled, which skips the commit
        and leaves the document as an uncommitted change.  An empty answer
        falls back to the date/time stamp.
        """
        paths = [str(value) for value in relative_paths]
        remembered = str(self.context.settings.get("custom_commit_message", "") or "").strip()
        if len(paths) == 1:
            name = Path(paths[0]).name
            label = "Commit message for {}:".format(name)
            initial = remembered or name
        elif paths:
            label = "Commit message for {} documents:".format(len(paths))
            initial = remembered
        else:
            label = "Commit message:"
            initial = remembered
        return self._ask_text("GitSync commit message", label, initial)

    def remember_commit_message(self, message: str) -> None:
        """Keep the last message as the prefill and for the exit commit."""
        text = str(message or "").strip()
        if not text:
            return
        values = dict(self.context.settings)
        values["custom_commit_message"] = text
        try:
            self.context.settings = self.context.settings_store.save(values)
        except Exception as error:
            self._show_error(error)

    def commit_message_cancelled(self, relative_paths) -> None:
        names = ", ".join(Path(str(value)).name for value in relative_paths)
        self._set_status(
            "Save not committed: {} (commit message cancelled)".format(names)
        )

    def prepare_exit_commit(self, relative_paths) -> Dict[str, Any]:
        """Commit only open/saved repository documents and their PNGs."""
        repository = self._repository()
        if repository is None:
            return {"commit": None, "previews": {"paths": [], "errors": []}}
        preview_result = self.prepare_commit_previews(relative_paths)
        for failure in preview_result.get("errors", []):
            print(
                "GitSync: preview not updated for {}: {}".format(
                    failure.get("path", ""), failure.get("error", "")
                )
            )
        commit_paths = sorted({str(path) for path in relative_paths if path} | set(preview_result["paths"]))
        if not commit_paths:
            return {"commit": None, "previews": preview_result}
        message = build_commit_message(
            self.context.settings, relative_paths, prefix="Exit save"
        )
        commit = repository.commit_paths(commit_paths, message)
        return {"commit": commit, "previews": preview_result}

    # ------------------------------------------------------------------
    # Git actions
    # ------------------------------------------------------------------
    def pull(self) -> None:
        if self.context.runner.busy:
            return
        repository = self._repository()
        if repository is None:
            return
        if not self._confirm_insecure_operation("Pull"):
            return
        self._set_status("Pulling remote changes…")
        self._submit(repository.pull, on_success=lambda _result: self.refresh(warn_insecure=False), on_error=self._show_error)

    def push(self, skip_insecure_confirmation: bool = False) -> None:
        if self.context.runner.busy:
            return
        repository = self._repository()
        if repository is None:
            return
        if not skip_insecure_confirmation and not self._confirm_insecure_operation("Push"):
            return
        self._set_status("Pushing local commits…")
        self._submit(
            repository.push,
            on_success=lambda _result: self.refresh(warn_insecure=False),
            on_error=self._show_error,
        )

    def commit_working_tree(self, entries=None) -> None:
        """Commit every working-tree change with the automatic message.

        This is the escape hatch for files GitSync did not create from a
        document save, for example a model copied into the workspace.
        """
        if self.context.runner.busy:
            return
        repository = self._repository()
        if repository is None:
            return
        if not self._confirm_insecure_operation("Commit"):
            return
        push_after_commit = bool(self.context.settings.get("auto_push_save", False))
        if push_after_commit and not self._confirm_insecure_operation("Push"):
            push_after_commit = False
        candidates: List[str] = []
        for entry in entries or ():
            if isinstance(entry, dict):
                related = entry.get("related_paths") or [entry.get("path", "")]
                candidates.extend(str(value) for value in related if value)
            elif entry:
                candidates.append(str(entry))
        try:
            preview_result = self.prepare_commit_previews(candidates)
        except Exception as error:
            self._show_error(error)
            return
        for failure in preview_result.get("errors", []):
            print(
                "GitSync: preview not updated for {}: {}".format(
                    failure.get("path", ""), failure.get("error", "")
                )
            )
        message = build_commit_message(self.context.settings, candidates, prefix="GitSync")
        self._set_status("Committing working-tree changes…")

        def operation():
            result = repository.commit_all(message)
            result["pushed"] = False
            if result.get("created") and push_after_commit:
                result["pushed"] = repository.push().ok
            return result

        def success(result: Dict[str, Any]) -> None:
            # The exit pass only commits documents the automatic commits did
            # not already handle.
            controller = getattr(self.context, "controller", None)
            if result.get("created") and controller is not None:
                if hasattr(controller, "forget_session_paths"):
                    controller.forget_session_paths()
            if result.get("pushed"):
                self._set_status("Commit created and pushed.")
            elif result.get("created"):
                self._set_status("Commit created.")
            else:
                self._set_status("No changes to commit.")
            self._startup_checked = False
            self.refresh(self._startup_check_after_refresh, warn_insecure=False)

        self._submit(operation, on_success=success, on_error=self._show_error)

    def auto_sync_preparing(self) -> None:
        """Announce the GUI-thread preview step that precedes an auto commit."""
        self._set_status("Generating preview and committing saved document…")

    def auto_sync_finished(self, result: Dict[str, Any]) -> None:
        commit = result.get("commit", {})
        pushed = bool(result.get("push"))
        if commit.get("created"):
            self._set_status(
                "Saved document committed and pushed."
                if pushed
                else "Saved document committed; previews updated."
            )
        else:
            self._set_status(
                "No document changes; existing commits were pushed."
                if pushed
                else "No document changes to commit."
            )
        self.refresh(warn_insecure=False)

    def auto_sync_failed(self, error: BaseException) -> None:
        self._set_status("Automatic save sync failed: {}".format(str(error)))
        self._show_error(error)

    # ------------------------------------------------------------------
    # Model preview/open
    # ------------------------------------------------------------------
    def _selection_changed(self) -> None:
        relative = self._selected_model_path()
        self.open_model_button.setEnabled(bool(relative))
        if not relative:
            set_plain_text(self.preview_path_label, "Select a model to preview")
            set_plain_text(self.preview_label, "Select a model file")
            return
        set_plain_text(self.preview_path_label, relative)
        self._render_preview(relative)

    def _repository_is_trusted(self) -> bool:
        """True when the user granted this repository a permanent exemption."""
        settings = self.context.settings
        trusted = str(settings.get("trusted_repository", "") or "").strip()
        if not trusted:
            return False
        target = canonical_remote_target(
            settings.get("server_url", ""), settings.get("username", "")
        )
        return bool(target) and trusted == target

    def trust_current_repository(self, trusted: bool = True) -> None:
        """Remember (or forget) that this repository may open its documents."""
        values = dict(self.context.settings)
        target = canonical_remote_target(
            values.get("server_url", ""), values.get("username", "")
        )
        if not trusted or not target:
            values["trusted_repository"] = ""
        else:
            values["trusted_repository"] = target
        try:
            self.context.settings = self.context.settings_store.save(values)
        except Exception as error:
            self._show_error(error)
            return
        self._preview_warning_shown = bool(trusted) or self._preview_warning_shown

    def _confirm_fcstd_preview(self, relative: str) -> bool:
        if Path(relative).suffix.lower() != ".fcstd" or self._preview_warning_shown:
            return True
        if relative in self._locally_created_paths:
            # The document was created in this session, so it cannot have come
            # from the repository.
            return True
        if self._repository_is_trusted():
            return True
        answer = show_trust_prompt(
            self,
            message_icon("Warning"),
            "Open FreeCAD document",
            "Opening a FreeCAD document can execute content embedded in that "
            "document.\n\nOnly preview documents from repositories you trust. "
            "Continue?",
        )
        if answer == "trust":
            self.trust_current_repository(True)
            return True
        if answer == "yes":
            self._preview_warning_shown = True
            return True
        return False

    def _render_preview(self, relative: str) -> None:
        if not self._confirm_fcstd_preview(relative):
            return
        repository = self._repository()
        if repository is None:
            return
        path = str(repository.root / relative)
        if not is_supported_model(path):
            set_plain_text(self.preview_label, "Unsupported preview format")
            return
        set_plain_text(self.preview_label, "Rendering preview…")
        QtWidgets.QApplication.setOverrideCursor(wait_cursor())
        try:
            pixmap, generated = self._preview.ensure_png(
                path,
                allowed_root=str(repository.root),
                force=False,
            )
            if generated:
                self._set_status("Preview saved beside the model in the repository.")
            target_size = self.preview_label.size()
            if target_size.width() < 1 or target_size.height() < 1:
                target_size = QtCore.QSize(640, 420)
            scaled = pixmap.scaled(
                target_size,
                keep_aspect_ratio(),
                smooth_transformation(),
            )
            self.preview_label.setPixmap(scaled)
        except Exception as exc:
            set_plain_text(self.preview_label, "Preview unavailable\n{}".format(exc))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def open_selected_model(self) -> None:
        relative = self._selected_model_path()
        repository = self._repository()
        if not relative or repository is None:
            return
        try:
            if not self._confirm_fcstd_preview(relative):
                return
            ModelPreview.open_model(
                str(repository.root / relative),
                allowed_root=str(repository.root),
            )
        except Exception as exc:
            show_plain_message(self, message_icon("Warning"), "GitSync", str(exc))

    def _model_context_menu(self, position) -> None:
        """Show the model list's right-click menu.

        Right-clicking a row also selects it, so the menu always acts on what
        was clicked rather than on a stale selection.  Folder rows carry no
        model path, so they get no menu.
        """
        item = self.models_tree.itemAt(position)
        if item is None:
            return
        relative = item.data(0, user_role())
        if not relative:
            return
        self.models_tree.setCurrentItem(item)
        menu = QtWidgets.QMenu(self)
        menu.addAction("Open Model").triggered.connect(self.open_selected_model)
        menu.addAction("Open in File Browser").triggered.connect(
            self.open_selected_in_file_manager
        )
        menu.addSeparator()
        menu.addAction("Rename…").triggered.connect(self.rename_selected_model)
        menu.addAction("Delete").triggered.connect(self.delete_selected_model)
        menu.exec(self.models_tree.viewport().mapToGlobal(position))

    @staticmethod
    def _document_open_at(path: Path) -> bool:
        """True when FreeCAD currently holds a document for this file.

        Renaming or deleting underneath an open document would be silently
        undone by its next save, which still writes to the original path.
        """
        if App is None:  # pragma: no cover - only outside FreeCAD
            return False
        try:
            documents = App.listDocuments()
        except Exception:
            documents = getattr(App, "getDocuments", lambda: {})()
        try:
            target = str(Path(path).resolve())
            for document in documents.values():
                name = str(getattr(document, "FileName", "") or "")
                if name and str(Path(name).resolve()) == target:
                    return True
        except Exception:
            return False
        return False

    def _selected_model_file(self) -> Optional[Path]:
        """Return the selected model's validated absolute path, or None.

        Reports the reason itself when the path is not usable, so the callers
        only have to handle the None case.
        """
        relative = self._selected_model_path()
        repository = self._repository()
        if not relative or repository is None:
            return None
        try:
            # The same containment and symlink checks previewing uses.
            return ModelPreview.validate_model_path(
                str(repository.root / relative), str(repository.root)
            )
        except Exception as exc:
            show_plain_message(self, message_icon("Warning"), "GitSync", str(exc))
            return None

    def _push_after_commit(self) -> bool:
        """True when a commit should be followed by a push, consent included."""
        if not self.context.settings.get("auto_push_save", False):
            return False
        # A commit is purely local, so only the push needs the transport
        # confirmation.  Declining leaves the commit in place.
        return self._confirm_insecure_operation("Push")

    def rename_selected_model(self) -> None:
        """Rename the selected model, and commit the rename."""
        if self.context.runner.busy:
            return
        repository = self._repository()
        model = self._selected_model_file()
        if model is None or repository is None:
            return
        relative = self._selected_model_path() or ""
        if self._document_open_at(model):
            show_plain_message(
                self,
                message_icon("Warning"),
                "GitSync",
                "Close {} in FreeCAD before renaming it.\n\n"
                "An open document still saves to its original path, which "
                "would undo the rename.".format(model.name),
            )
            return
        text, accepted = QtWidgets.QInputDialog.getText(
            self,
            "Rename model",
            "New file name (the model stays in its folder):",
            line_edit_normal_mode(),
            model.name,
        )
        if not accepted or not str(text).strip():
            return
        try:
            _model, target = rename_target(model, text)
            pairs = rename_pairs(model, target)
        except Exception as exc:
            self._show_error(exc)
            return
        moves = [
            (
                repository.relative_path(old),
                repository.relative_path(new),
            )
            for old, new in pairs
        ]
        message = "{} renamed".format(commit_timestamp())
        push_after = self._push_after_commit()
        new_relative = str(Path(relative).with_name(target.name))
        self._locally_created_paths.discard(relative)
        self._select_after_refresh = new_relative
        self._set_status("Renaming {}…".format(model.name))

        def operation():
            result = repository.move_paths(moves, message)
            result["pushed"] = False
            if result.get("created") and push_after:
                result["pushed"] = repository.push().ok
            return result

        def success(result: Dict[str, Any]) -> None:
            if not result.get("created"):
                self._set_status("Nothing to commit for the rename.")
            elif result.get("pushed"):
                self._set_status(
                    "Renamed to {} and pushed.".format(target.name)
                )
            else:
                self._set_status(
                    "Renamed to {} and committed it.".format(target.name)
                )
            self.refresh(warn_insecure=False)

        self._submit(operation, on_success=success, on_error=self._show_error)

    def delete_selected_model(self) -> None:
        """Delete the selected model and its preview, and commit the removal."""
        if self.context.runner.busy:
            return
        repository = self._repository()
        model = self._selected_model_file()
        if model is None or repository is None:
            return
        relative = self._selected_model_path() or ""
        if self._document_open_at(model):
            show_plain_message(
                self,
                message_icon("Warning"),
                "GitSync",
                "Close {} in FreeCAD before deleting it.\n\n"
                "An open document would be saved again on its next save.".format(
                    model.name
                ),
            )
            return
        answer = show_plain_message(
            self,
            message_icon("Warning"),
            "Delete model",
            "Delete {} from the repository?\n\nIts preview PNG is deleted too. "
            "Git refuses to delete a file whose changes were never committed, so "
            "those edits are safe.".format(model.name),
            message_buttons(message_yes(), message_no()),
            message_no(),
        )
        if answer != message_yes():
            return
        try:
            targets = removal_paths(model)
        except Exception as exc:
            self._show_error(exc)
            return
        paths = [repository.relative_path(target) for target in targets]
        message = "{} deleted".format(commit_timestamp())
        push_after = self._push_after_commit()
        self._locally_created_paths.discard(relative)
        self._select_after_refresh = None
        self._set_status("Deleting {}…".format(model.name))

        def operation():
            result = repository.remove_paths(paths, message)
            result["pushed"] = False
            if result.get("created") and push_after:
                result["pushed"] = repository.push().ok
            return result

        def success(result: Dict[str, Any]) -> None:
            if not result.get("created"):
                self._set_status(
                    "{} was removed from disk; Git had nothing to record.".format(
                        model.name
                    )
                )
            elif result.get("pushed"):
                self._set_status("Deleted {} and pushed.".format(model.name))
            else:
                self._set_status("Deleted {} and committed it.".format(model.name))
            self.refresh(warn_insecure=False)

        self._submit(operation, on_success=success, on_error=self._show_error)

    def open_selected_in_file_manager(self) -> None:
        """Show the selected model in the operating system's file browser."""
        relative = self._selected_model_path()
        repository = self._repository()
        if not relative or repository is None:
            return
        try:
            # Same checks as previewing: a path outside the workspace, or one
            # reached through a symlink, must never reach an external program.
            model = ModelPreview.validate_model_path(
                str(repository.root / relative), str(repository.root)
            )
            command = reveal_in_file_manager(model)
        except Exception as exc:
            show_plain_message(self, message_icon("Warning"), "GitSync", str(exc))
            return
        self._set_status("Opened in the file browser: {}".format(command))

    # ------------------------------------------------------------------
    # New document
    # ------------------------------------------------------------------
    def _default_new_model_name(self) -> str:
        """Suggest a model name that is not used in the workspace yet.

        Returned without the ``.FCStd`` suffix, because that is what the prompt
        shows and what the user edits; ``normalized_model_filename()`` appends
        the suffix when the document is created.  An existing ``NewModel.FCStd``
        still counts as "NewModel" being taken, so the *stems* are compared.
        """
        existing = set()
        repository = self._repository()
        if repository is not None:
            try:
                existing = {
                    child.stem.lower()
                    for child in repository.root.iterdir()
                    if child.is_file() and not child.is_symlink()
                }
            except OSError:
                existing = set()
        for index in range(1, 1000):
            candidate = "NewModel" if index == 1 else "NewModel{}".format(index)
            if candidate.lower() not in existing:
                return candidate
        return "NewModel"

    def _new_model_target(self, name: str):
        """Return a validated, unused file name and its absolute path."""
        filename = normalized_model_filename(name)
        repository = self._repository()
        if repository is None:
            raise GitError("Connect to a repository before creating a new model.")
        target = repository.root / filename
        if not repository.contains_path(str(target)):
            raise GitError("The new document must be created inside the repository.")
        if target.is_symlink() or target.exists():
            raise GitError("{} already exists in the repository.".format(filename))
        # Case-folding file systems would treat "Rack.FCStd" and "rack.FCStd"
        # as the same file, so compare the name case-insensitively as well.
        try:
            siblings = {child.name.lower() for child in target.parent.iterdir()}
        except OSError as exc:
            raise GitError("Unable to inspect the workspace: {}".format(exc))
        if filename.lower() in siblings:
            raise GitError("{} already exists in the repository.".format(filename))
        return filename, target

    def new_model(self) -> None:
        """Create a new FreeCAD document and save it inside the workspace."""
        if self.context.runner.busy:
            return
        repository = self._repository()
        if repository is None or not repository.is_repository():
            self._set_status("Connect to a repository before creating a new model.")
            return
        if App is None:  # pragma: no cover - only reachable outside FreeCAD
            self._set_status("FreeCAD document creation is unavailable.")
            return
        text, accepted = QtWidgets.QInputDialog.getText(
            self,
            "New model",
            # No ".FCStd" here: the suffix is added when the file is created,
            # so the prompt shows only what the user has to think about.
            "Model name inside the repository (.FCStd is added):",
            line_edit_normal_mode(),
            self._default_new_model_name(),
        )
        if not accepted or not str(text).strip():
            return
        try:
            filename, target = self._new_model_target(text)
        except GitError as error:
            self._show_error(error)
            return
        try:
            document = App.newDocument(Path(filename).stem)
        except Exception as error:
            self._show_error(error)
            return
        try:
            document.saveAs(str(target))
        except Exception as error:
            # Never leave a half-written file in the repository.
            try:
                if target.is_file() and not target.is_symlink():
                    target.unlink()
            except OSError:
                pass
            try:
                App.closeDocument(document.Name)
            except Exception:
                pass
            self._show_error(error)
            return
        if not target.is_file():
            self._set_status("The new document was not written to the repository.")
            return
        self._locally_created_paths.add(filename)
        self._activate_document(document.Name)
        self._select_after_refresh = filename
        if self.context.settings.get("auto_commit_save", True):
            self._set_status("Created {}. Committing it with its preview…".format(filename))
        else:
            self._set_status(
                "Created {}. It is uncommitted until you commit it.".format(filename)
            )
        self._schedule_new_model_refresh(remaining=20)

    def _activate_document(self, name: str) -> None:
        """Bring a freshly created document to the front for editing."""
        try:
            if App is not None and hasattr(App, "setActiveDocument"):
                App.setActiveDocument(name)
        except Exception:
            pass
        try:
            if Gui is not None:
                document = Gui.getDocument(name)
                if document is not None:
                    Gui.ActiveDocument = document
        except Exception:
            pass

    def _schedule_new_model_refresh(self, remaining: int = 20) -> None:
        """Refresh once the automatic save commit is no longer running."""
        if remaining <= 0:
            return
        try:
            if self.context.runner.busy:
                QtCore.QTimer.singleShot(
                    400, lambda: self._schedule_new_model_refresh(remaining - 1)
                )
                return
            self.refresh(warn_insecure=False)
        except RuntimeError:
            # The panel was closed while the timer was still pending.
            pass


__all__ = ["SyncPanel"]
