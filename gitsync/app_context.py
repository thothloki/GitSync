"""Shared state for the GitSync workbench GUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .async_tasks import TaskRunner
from .configuration import SettingsStore
from .git_service import GitRepository, canonical_remote_target


class GitSyncContext:
    """Own the settings, repository object, and serialized task runner."""

    def __init__(self) -> None:
        self.settings_store = SettingsStore()
        self.settings: Dict[str, Any] = self.settings_store.load()
        self.runner = TaskRunner()
        self.repository: Optional[GitRepository] = None
        self.generation = 0
        self.panel = None
        self.controller = None
        self.reload_repository()

    def reload_repository(self) -> Optional[GitRepository]:
        workspace = str(self.settings.get("workspace", "")).strip()
        if not workspace:
            self.repository = None
            return None
        self.repository = GitRepository(
            workspace=workspace,
            remote_url=str(self.settings.get("server_url", "")),
            username=str(self.settings.get("username", "")),
            token=str(self.settings.get("token", "")),
        )
        return self.repository

    def save_settings(
        self,
        values: Dict[str, Any],
        repository: Optional[GitRepository] = None,
    ) -> Dict[str, Any]:
        """Persist settings, optionally activating an already-connected repo.

        Connection setup uses the optional repository argument so a failed
        clone/remote validation never replaces the active workspace or
        credentials.
        """
        old_repository = self.repository
        old_target = canonical_remote_target(
            self.settings.get("server_url", ""), self.settings.get("username", "")
        )
        self.settings = self.settings_store.save(values)
        if repository is None:
            self.reload_repository()
        else:
            repository.update_credentials(
                username=str(values.get("username", "")),
                token=str(values.get("token", "")),
                remote_url=str(values.get("server_url", "")),
            )
            self.repository = repository
            self.settings["workspace"] = str(repository.root)
            self.settings["server_url"] = repository.remote_url
        new_target = canonical_remote_target(
            self.settings.get("server_url", ""), self.settings.get("username", "")
        )
        if (
            old_repository is not self.repository
            or old_target != new_target
            or str(getattr(old_repository, "root", "")) != str(getattr(self.repository, "root", ""))
        ):
            self.generation += 1
        if self.controller is not None:
            self.controller.settings_changed()
        return self.settings

    def set_repository(self, repository: GitRepository) -> None:
        if self.repository is not repository:
            self.generation += 1
        self.repository = repository
        self.settings["workspace"] = str(repository.root)
        self.settings["server_url"] = repository.remote_url

    def configured_workspace(self) -> Optional[Path]:
        value = str(self.settings.get("workspace", "")).strip()
        return Path(value).expanduser() if value else None


__all__ = ["GitSyncContext"]
