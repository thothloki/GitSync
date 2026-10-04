"""Qt dialogs used by GitSync."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .configuration import credential_fallback_warning
from .git_service import canonical_remote_target, validate_remote_url
from .model_list import normalize_sort_order
from .qt_compat import (
    QtWidgets,
    button_cancel,
    button_ok,
    expanding_policy,
    exec_dialog,
    fixed_policy,
    line_edit_normal_mode,
    line_edit_password_mode,
    message_buttons,
    message_icon,
    message_no,
    message_yes,
    set_plain_text,
    show_plain_message,
    text_selectable_by_mouse,
)


class ConnectionForm(QtWidgets.QWidget):
    """Reusable repository URL, credential, and workspace editor."""

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
        super().__init__(parent)
        self._initial_target = ""
        self._token_cleared_for_target = False
        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("https://server/user/repository.git")
        self.username_edit = QtWidgets.QLineEdit()
        self.token_edit = QtWidgets.QLineEdit()
        self.token_edit.setEchoMode(line_edit_password_mode())
        self.token_edit.setPlaceholderText("Password or API token")
        self.workspace_edit = QtWidgets.QLineEdit()
        self.workspace_edit.setPlaceholderText("Choose the local Git working directory")
        self.show_token = QtWidgets.QCheckBox("Show token")
        self.show_token.toggled.connect(self._toggle_token)
        self.url_edit.textChanged.connect(self._target_changed)
        self.username_edit.textChanged.connect(self._target_changed)

        browse_button = QtWidgets.QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_workspace)
        workspace_row = QtWidgets.QHBoxLayout()
        workspace_row.addWidget(self.workspace_edit, 1)
        workspace_row.addWidget(browse_button)

        form = QtWidgets.QFormLayout(self)
        form.addRow("Repository URL", self.url_edit)
        form.addRow("Username", self.username_edit)
        token_row = QtWidgets.QHBoxLayout()
        token_row.addWidget(self.token_edit, 1)
        token_row.addWidget(self.show_token)
        form.addRow("Password / API token", token_row)
        form.addRow("Local workspace", workspace_row)

        note = QtWidgets.QLabel(
            "The workspace is the local Git clone. GitSync will not overwrite a "
            "non-empty directory that is not already a repository."
        )
        note.setWordWrap(True)
        form.addRow("", note)

    def _target_changed(self, *_args) -> None:
        current = canonical_remote_target(self.url_edit.text(), self.username_edit.text())
        if self._initial_target and current != self._initial_target and not self._token_cleared_for_target:
            self.token_edit.clear()
            self._token_cleared_for_target = True

    def _toggle_token(self, checked: bool) -> None:
        mode = line_edit_normal_mode() if checked else line_edit_password_mode()
        self.token_edit.setEchoMode(mode)

    def _browse_workspace(self) -> None:
        current = self.workspace_edit.text().strip()
        start = current or str(Path.home())
        selected = QtWidgets.QFileDialog.getExistingDirectory(self, "Select local Git workspace", start)
        if selected:
            self.workspace_edit.setText(selected)

    def set_values(self, values: Dict[str, Any]) -> None:
        widgets = (self.url_edit, self.username_edit, self.token_edit, self.workspace_edit)
        for widget in widgets:
            widget.blockSignals(True)
        self.url_edit.setText(str(values.get("server_url", "")))
        self.username_edit.setText(str(values.get("username", "")))
        self.token_edit.setText(str(values.get("token", "")))
        self.workspace_edit.setText(str(values.get("workspace", "")))
        for widget in widgets:
            widget.blockSignals(False)
        self._initial_target = canonical_remote_target(
            self.url_edit.text(), self.username_edit.text()
        )
        self._token_cleared_for_target = False

    def values(self) -> Dict[str, str]:
        return {
            "server_url": self.url_edit.text().strip(),
            "username": self.username_edit.text().strip(),
            "token": self.token_edit.text(),
            "workspace": self.workspace_edit.text().strip(),
        }

    def validate(
        self,
        parent: QtWidgets.QWidget,
        warn_http: bool = True,
        require_connection: bool = True,
        allow_insecure: bool = False,
    ) -> bool:
        values = self.values()
        if not require_connection and not values["server_url"] and not values["workspace"]:
            return True
        try:
            validate_remote_url(values["server_url"])
        except ValueError as exc:
            show_plain_message(parent, message_icon("Critical"), "GitSync setup", str(exc))
            self.url_edit.setFocus()
            return False
        if not values["workspace"]:
            show_plain_message(parent, message_icon("Critical"), "GitSync setup", "Choose a local workspace directory.")
            self.workspace_edit.setFocus()
            return False
        if warn_http and not allow_insecure and values["server_url"].lower().startswith("http://"):
            answer = show_plain_message(
                parent,
                message_icon("Warning"),
                "Insecure repository connection",
                "This URL uses plain HTTP. Credentials and repository data will not be encrypted in transit.\n\n"
                "Continue anyway?",
                message_buttons(message_yes(), message_no()),
                message_no(),
            )
            if answer != message_yes():
                return False
        return True


class SetupDialog(QtWidgets.QDialog):
    """First-run or reconnect dialog."""

    def __init__(
        self,
        values: Optional[Dict[str, Any]] = None,
        parent: Optional[QtWidgets.QWidget] = None,
        allow_insecure: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("GitSync Setup")
        self.setModal(True)
        self.allow_insecure = allow_insecure
        self.form = ConnectionForm(self)
        self.form.set_values(values or {})

        self.buttons = QtWidgets.QDialogButtonBox(button_ok() | button_cancel(), parent=self)
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.form)
        layout.addWidget(self.buttons)
        self.resize(620, 260)

    def _accept_if_valid(self) -> None:
        if self.form.validate(self, allow_insecure=self.allow_insecure):
            self.accept()

    def values(self) -> Dict[str, str]:
        return self.form.values()


class SettingsDialog(QtWidgets.QDialog):
    """Connection settings plus behavior and security checkboxes."""

    def __init__(
        self,
        values: Dict[str, Any],
        settings_store: Any,
        parent: Optional[QtWidgets.QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("GitSync Settings")
        self.setModal(True)
        self.form = ConnectionForm(self)
        self.form.set_values(values)

        self.preview_home = QtWidgets.QCheckBox("Set the 3D view to Home for a better preview")
        self.preview_home.setChecked(bool(values.get("preview_home", True)))
        self.auto_commit_save = QtWidgets.QCheckBox("Commit saved documents automatically")
        self.auto_commit_save.setChecked(bool(values.get("auto_commit_save", True)))
        self.auto_push_save = QtWidgets.QCheckBox("Push after each automatic commit")
        self.auto_push_save.setChecked(bool(values.get("auto_push_save", False)))

        self.custom_commit_message = QtWidgets.QCheckBox("Ask me for the commit message on save")
        self.custom_commit_message.setToolTip(
            "Every time a document is saved, a prompt asks for the commit message.\n\n"
            "Press Enter to accept the pre-filled text, clear the field to use the\n"
            "date/time stamp, or cancel to leave the document uncommitted.\n"
            "The last message is reused as the pre-filled text and for the commit\n"
            "made when FreeCAD exits."
        )
        self.custom_commit_message.setChecked(
            str(values.get("auto_commit_message", "timestamp")).lower() == "custom"
        )
        # The last typed message belongs to the automatic commits, not to this
        # dialog: it is only passed through so saving settings keeps it.
        self._remembered_commit_message = str(
            values.get("custom_commit_message", "") or ""
        ).strip()
        # The model list order is chosen in the panel, so it is passed through
        # the same way.  values() reads both of these, so they must exist before
        # the dialog can be accepted.
        self._model_sort = normalize_sort_order(values.get("model_sort", "name"))
        self.push_on_exit = QtWidgets.QCheckBox(
            "Commit open documents and push when FreeCAD exits"
        )
        self.push_on_exit.setToolTip(
            "On exit, commit only the documents saved or still open in this session, then push."
        )
        self.push_on_exit.setChecked(bool(values.get("push_on_exit", False)))
        self.allow_insecure_http = QtWidgets.QCheckBox(
            "Allow unencrypted HTTP operations without confirmation"
        )
        self.allow_insecure_http.setToolTip(
            "Suppress insecure-connection warnings for pull, refresh, commit, and push."
        )
        self.allow_insecure_http.setChecked(bool(values.get("allow_insecure_http", False)))

        self._connection_target = canonical_remote_target(
            values.get("server_url", ""), values.get("username", "")
        )
        self.trust_repository = QtWidgets.QCheckBox(
            "Trust model files in this repository"
        )
        self.trust_repository.setToolTip(
            "Skip the FreeCAD document warning when previewing or opening .FCStd files.\n\n"
            "The trust applies only to the repository URL entered above and is "
            "removed when you change it."
        )
        self.trust_repository.setChecked(
            bool(values.get("trusted_repository", ""))
            and self._connection_target
            and str(values.get("trusted_repository", "")).strip() == self._connection_target
        )

        warning = credential_fallback_warning(settings_store)
        storage_label = QtWidgets.QLabel(
            settings_store.credentials.status_text
            if not warning
            else "Warning: " + warning
        )
        storage_label.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(button_ok() | button_cancel(), parent=self)
        self.buttons.accepted.connect(self._accept_if_valid)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.form)
        layout.addWidget(self.preview_home)
        layout.addWidget(self.auto_commit_save)
        layout.addWidget(self.auto_push_save)
        layout.addWidget(self.custom_commit_message)
        layout.addWidget(self.push_on_exit)
        layout.addWidget(self.allow_insecure_http)
        layout.addWidget(self.trust_repository)
        layout.addWidget(storage_label)
        layout.addWidget(self.buttons)
        # Tall enough for the connection form, its hint text, and six
        # checkboxes without clipping anything on a normal display.
        self.resize(660, 510)

    def _accept_if_valid(self) -> None:
        if self.form.validate(
            self,
            require_connection=False,
            allow_insecure=self.allow_insecure_http.isChecked(),
        ):
            self.accept()

    def values(self) -> Dict[str, Any]:
        values: Dict[str, Any] = self.form.values()
        values.update(
            {
                "preview_home": self.preview_home.isChecked(),
                "auto_commit_save": self.auto_commit_save.isChecked(),
                "auto_push_save": self.auto_push_save.isChecked(),
                "auto_commit_message": (
                    "custom" if self.custom_commit_message.isChecked() else "timestamp"
                ),
                # The last typed message is kept so it can be pre-filled and
                # reused by the exit commit; the dialog never edits it.
                "custom_commit_message": self._remembered_commit_message,
                # The model list order is selected in the panel.
                "model_sort": self._model_sort,
                "push_on_exit": self.push_on_exit.isChecked(),
                "allow_insecure_http": self.allow_insecure_http.isChecked(),
            }
        )
        # Trust is granted for the target that was shown when the dialog
        # opened.  If the URL or user name was edited, the choice is dropped
        # instead of being transferred to a repository the user has not seen.
        target = canonical_remote_target(
            values.get("server_url", ""), values.get("username", "")
        )
        if self.trust_repository.isChecked() and target and target == self._connection_target:
            values["trusted_repository"] = target
        else:
            values["trusted_repository"] = ""
        return values


class StartupSyncDialog(QtWidgets.QDialog):
    """Show the local/remote comparison and let the user choose a safe action."""

    #: Entries shown before the list is summarised with a "... and N more" row.
    _CHANGES_LIMIT = 200

    def __init__(
        self,
        summary: Dict[str, Any],
        parent: Optional[QtWidgets.QWidget] = None,
        error: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("GitSync Startup Check")
        self.setModal(True)
        self.choice: Optional[str] = None
        self.summary = summary or {}
        self.error = error

        layout = QtWidgets.QVBoxLayout(self)
        if error:
            heading = QtWidgets.QLabel("The remote repository could not be checked.")
            heading.setStyleSheet("font-weight: bold;")
            layout.addWidget(heading)
            details = QtWidgets.QLabel()
            details.setWordWrap(True)
            set_plain_text(details, error)
            details.setTextInteractionFlags(text_selectable_by_mouse())
            layout.addWidget(details)
            self._add_button("Retry", "retry")
            self._add_button("Continue without syncing", "close")
        else:
            layout.addWidget(self._summary_label())
            self._add_commit_list(layout, "Local-only commits:", self.summary.get("local_commits", []))
            self._add_file_list(layout, "Files in local-only commits:", self.summary.get("local_files", []))
            self._add_commit_list(layout, "Remote-only commits:", self.summary.get("remote_commits", []))
            self._add_file_list(layout, "Files in remote-only commits:", self.summary.get("remote_files", []))
            if self.summary.get("entries"):
                layout.addWidget(QtWidgets.QLabel("Uncommitted working-tree changes:"))
                changes = QtWidgets.QListWidget()
                # The uncommitted list is the reason the dialog is open, so it
                # takes all the space the summaries above do not need.
                changes.setMinimumHeight(180)
                for row in self._changes_rows(self.summary["entries"]):
                    changes.addItem(row)
                layout.addWidget(changes, 1)
            else:
                # Nothing to commit: let the last summary list take the room
                # instead of leaving the bottom of the dialog empty.
                self._stretch_last_list(layout)
            self._add_actions()
        self.resize(700, 620)

    @staticmethod
    def _stretch_last_list(layout) -> None:
        for index in range(layout.count() - 1, -1, -1):
            widget = layout.itemAt(index).widget()
            if isinstance(widget, QtWidgets.QListWidget):
                layout.setStretch(index, 1)
                widget.setMinimumHeight(180)
                return

    @classmethod
    def _changes_rows(cls, entries) -> List[str]:
        """Return the uncommitted-change rows, with a summary row when clipped."""
        values = list(entries or [])
        rows = [
            "{}  {}".format(entry.get("status", ""), entry.get("path", ""))
            for entry in values[: cls._CHANGES_LIMIT]
        ]
        remaining = len(values) - cls._CHANGES_LIMIT
        if remaining > 0:
            rows.append("... and {} more change(s)".format(remaining))
        return rows

    def _summary_label(self) -> QtWidgets.QLabel:
        branch = self.summary.get("branch", "unknown branch")
        upstream = self.summary.get("upstream") or "no upstream"
        ahead = int(self.summary.get("ahead", 0) or 0)
        behind = int(self.summary.get("behind", 0) or 0)
        entries = self.summary.get("entries", [])
        if entries and ahead == 0 and behind == 0:
            text = "Local branch: {}\nWorking tree: uncommitted changes".format(branch)
        elif ahead and behind:
            text = "Local branch: {}\n{} local commit(s) and {} remote commit(s) are pending.".format(
                branch, ahead, behind
            )
        elif ahead:
            text = "Local branch: {}\n{} local commit(s) are waiting to be pushed.".format(branch, ahead)
        elif behind:
            text = "Local branch: {}\n{} remote commit(s) are available to pull.".format(branch, behind)
        else:
            text = "Local branch: {}\nThe local working tree is synchronized with {}.".format(
                branch, upstream
            )
        label = QtWidgets.QLabel()
        label.setWordWrap(True)
        set_plain_text(label, text)
        return label

    def _add_commit_list(self, layout, title: str, commits) -> None:
        if not commits:
            return
        layout.addWidget(QtWidgets.QLabel(title))
        commit_list = QtWidgets.QListWidget()
        commit_list.setMaximumHeight(100)
        for commit in commits[:100]:
            commit_list.addItem(str(commit))
        layout.addWidget(commit_list)

    def _add_file_list(self, layout, title: str, files) -> None:
        if not files:
            return
        layout.addWidget(QtWidgets.QLabel(title))
        file_list = QtWidgets.QListWidget()
        file_list.setMaximumHeight(90)
        for path in files[:100]:
            file_list.addItem(str(path))
        layout.addWidget(file_list)

    def _add_actions(self) -> None:
        ahead = int(self.summary.get("ahead", 0) or 0)
        behind = int(self.summary.get("behind", 0) or 0)
        if self.summary.get("entries"):
            # Pulling, merging, or rebasing with a dirty working tree can
            # fail or create surprises, so the uncommitted files are handled
            # first with the configured automatic commit message.
            self._add_button("Commit the uncommitted changes", "commit")
        elif not self.summary.get("upstream") and self.summary.get("remote"):
            self._add_button("Set upstream to origin/{}".format(self.summary.get("branch", "main")), "upstream")
        elif ahead and not behind:
            self._add_button("Push local commits", "push")
        elif behind and not ahead:
            self._add_button("Pull remote changes", "pull")
        elif ahead and behind:
            self._add_button("Merge remote changes", "merge")
            self._add_button("Rebase local commits", "rebase")
        self._add_button("Continue without changing files", "close")

    def _add_button(self, text: str, choice: str) -> None:
        button = QtWidgets.QPushButton(text, self)
        button.clicked.connect(lambda _checked=False, value=choice: self._choose(value))
        # Keep the action buttons together at the bottom of the dialog.
        button.setSizePolicy(expanding_policy(), fixed_policy())
        self.layout().addWidget(button)

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.accept()

    @staticmethod
    def show_dialog(
        summary: Dict[str, Any],
        parent: Optional[QtWidgets.QWidget] = None,
        error: str = "",
    ) -> str:
        dialog = StartupSyncDialog(summary, parent, error)
        exec_dialog(dialog)
        return dialog.choice or "close"


__all__ = ["ConnectionForm", "SetupDialog", "SettingsDialog", "StartupSyncDialog"]
