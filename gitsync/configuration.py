"""Persistent configuration and credential handling for GitSync."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from .git_service import canonical_remote_target

try:  # FreeCAD is not available in standalone unit tests.
    import FreeCAD as App
except ImportError:  # pragma: no cover - exercised only outside FreeCAD
    App = None


# Manual verification runs can redirect the parameter group so they never
# overwrite the settings of a real FreeCAD installation.
PARAM_GROUP = os.environ.get("GITSYNC_PARAM_GROUP") or (
    "User parameter:BaseApp/Preferences/Mod/GitSync"
)
CREDENTIAL_SERVICE = "GitSync"

DEFAULTS: Dict[str, Any] = {
    "server_url": "",
    "username": "",
    "workspace": "",
    "preview_home": True,
    "auto_commit_save": True,
    "auto_push_save": False,
    "auto_commit_message": "timestamp",
    "custom_commit_message": "",
    "model_sort": "name",
    "push_on_exit": False,
    "allow_insecure_http": False,
    "trusted_repository": "",
}


class _MemoryParameters:
    """Small ParameterGrp-compatible object used by non-FreeCAD tests."""

    def __init__(self) -> None:
        self._values: Dict[str, Any] = {}

    def GetString(self, name: str, default: str = "") -> str:
        value = self._values.get(name, default)
        return str(value)

    def SetString(self, name: str, value: str) -> None:
        self._values[name] = str(value)

    def GetInt(self, name: str, default: int = 0) -> int:
        return int(self._values.get(name, default))

    def SetInt(self, name: str, value: int) -> None:
        self._values[name] = int(value)

    def RemString(self, name: str) -> None:
        self._values.pop(name, None)

    def RemInt(self, name: str) -> None:
        self._values.pop(name, None)


def _parameter_group() -> Any:
    if App is not None:
        return App.ParamGet(PARAM_GROUP)
    global _MEMORY_PARAMETERS
    try:
        return _MEMORY_PARAMETERS
    except NameError:
        _MEMORY_PARAMETERS = _MemoryParameters()
        return _MEMORY_PARAMETERS


def _save_parameters() -> None:
    if App is not None:
        try:
            App.saveParameter()
        except Exception:
            # ParameterGrp.Set* has already updated the in-memory values;
            # failing to flush the global file should not make a save fail.
            pass


def _normalized_account(username: str, server_url: str) -> str:
    """Build a stable keyring account without putting a secret in the key."""
    return "git-sync:{}".format(canonical_remote_target(server_url, username))


def _fallback_key(username: str, server_url: str) -> str:
    account = _normalized_account(username, server_url)
    digest = hashlib.sha256(account.encode("utf-8")).hexdigest()[:24]
    return "Credential_{}".format(digest)


def _safe_url_for_display(url: str) -> str:
    """Remove URL userinfo before displaying or logging a repository URL."""
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        hostname = parsed.hostname or ""
        if parsed.port:
            hostname = "{}:{}".format(hostname, parsed.port)
        return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))
    except Exception:
        return url


class CredentialStore:
    """Persist credentials in the OS keyring when possible.

    FreeCAD does not bundle a cross-platform credential store.  The optional
    ``keyring`` package is therefore used when it is available.  If it is not
    available, the token is kept in the FreeCAD parameter file as a practical
    persistence fallback and ``using_fallback`` is exposed to the UI so it can
    warn the user.  Tokens are never written to the repository URL or included
    in GitSync's error messages.
    """

    def __init__(self, parameters: Any = None) -> None:
        self.parameters = parameters or _parameter_group()
        self._keyring = None
        self._keyring_error: Optional[str] = None
        self._keyring_failed = False
        try:
            import keyring  # type: ignore

            self._keyring = keyring
        except Exception as exc:  # pragma: no cover - platform dependent
            self._keyring_error = str(exc)

    @property
    def using_fallback(self) -> bool:
        return self._keyring is None or self._keyring_failed

    @property
    def status_text(self) -> str:
        if self.using_fallback:
            return "The operating-system keyring is unavailable; credentials use unencrypted FreeCAD configuration storage."
        return "Credentials are stored in the operating-system keyring."

    def _account(self, username: str, server_url: str) -> str:
        return _normalized_account(username, server_url)

    def get(self, username: str, server_url: str) -> str:
        username = username or ""
        server_url = server_url or ""
        account = self._account(username, server_url)
        fallback = self.parameters.GetString(_fallback_key(username, server_url), "")
        if self._keyring_failed:
            return fallback
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(CREDENTIAL_SERVICE, account)
                if value:
                    return str(value)
                if fallback:
                    self._keyring_failed = True
            except Exception as exc:  # pragma: no cover - backend dependent
                self._keyring_error = str(exc)
                self._keyring_failed = True
                return fallback
        return fallback

    def set(self, username: str, server_url: str, token: str) -> None:
        username = username or ""
        server_url = server_url or ""
        account = self._account(username, server_url)
        if self._keyring is not None:
            try:
                if token:
                    self._keyring.set_password(CREDENTIAL_SERVICE, account, token)
                else:
                    try:
                        self._keyring.delete_password(CREDENTIAL_SERVICE, account)
                    except Exception:
                        pass
                # Do not retain an old fallback token after a successful secure
                # write.  It may have been created on a previous run.
                self.parameters.RemString(_fallback_key(username, server_url))
                self._keyring_failed = False
                return
            except Exception as exc:  # pragma: no cover - backend dependent
                self._keyring_error = str(exc)
                self._keyring_failed = True
        if token:
            self.parameters.SetString(_fallback_key(username, server_url), token)
        else:
            self.parameters.RemString(_fallback_key(username, server_url))

    def clear(self, username: str, server_url: str) -> None:
        self.set(username, server_url, "")


class SettingsStore:
    """Read/write GitSync settings while keeping the API small for the UI."""

    def __init__(self, parameters: Any = None) -> None:
        self.parameters = parameters or _parameter_group()
        self.credentials = CredentialStore(self.parameters)

    def load(self) -> Dict[str, Any]:
        params = self.parameters
        server_url = params.GetString("ServerURL", DEFAULTS["server_url"])
        username = params.GetString("Username", DEFAULTS["username"])
        return {
            "server_url": server_url,
            "username": username,
            "token": self.credentials.get(username, server_url),
            "workspace": params.GetString("Workspace", DEFAULTS["workspace"]),
            "preview_home": bool(params.GetInt("PreviewHome", int(DEFAULTS["preview_home"]))),
            "auto_commit_save": bool(
                params.GetInt("AutoCommitSave", int(DEFAULTS["auto_commit_save"]))
            ),
            "auto_push_save": bool(params.GetInt("AutoPushSave", int(DEFAULTS["auto_push_save"]))),
            "auto_commit_message": params.GetString(
                "AutoCommitMessage", DEFAULTS["auto_commit_message"]
            ),
            "custom_commit_message": params.GetString(
                "CustomCommitMessage", DEFAULTS["custom_commit_message"]
            ),
            "model_sort": params.GetString("ModelSort", DEFAULTS["model_sort"]),
            "push_on_exit": bool(params.GetInt("PushOnExit", int(DEFAULTS["push_on_exit"]))),
            "allow_insecure_http": bool(
                params.GetInt("AllowInsecureHTTP", int(DEFAULTS["allow_insecure_http"]))
            ),
            "trusted_repository": params.GetString(
                "TrustedRepository", DEFAULTS["trusted_repository"]
            ),
        }

    def save(self, values: Dict[str, Any]) -> Dict[str, Any]:
        params = self.parameters
        server_url = str(values.get("server_url", ""))
        username = str(values.get("username", ""))
        old = self.load()
        if canonical_remote_target(old.get("server_url", ""), old.get("username", "")) != canonical_remote_target(
            server_url, username
        ):
            # Do not leave a previous repository's token behind in the
            # fallback parameter store when the connection target changes.
            try:
                self.credentials.clear(old.get("username", ""), old.get("server_url", ""))
            except Exception:
                pass
        workspace_value = str(values.get("workspace", "")).strip()
        workspace_value = str(Path(workspace_value).expanduser().resolve()) if workspace_value else ""
        params.SetString("ServerURL", server_url)
        params.SetString("Username", username)
        params.SetString("Workspace", workspace_value)
        params.SetInt("PreviewHome", int(bool(values.get("preview_home", True))))
        params.SetInt(
            "AutoCommitSave",
            int(bool(values.get("auto_commit_save", True))),
        )
        params.SetInt("AutoPushSave", int(bool(values.get("auto_push_save", False))))
        message_mode = str(values.get("auto_commit_message", "timestamp")).lower()
        if message_mode not in {"timestamp", "custom"}:
            message_mode = "timestamp"
        params.SetString("AutoCommitMessage", message_mode)
        params.SetString(
            "CustomCommitMessage", str(values.get("custom_commit_message", "")).strip()
        )
        sort_order = str(values.get("model_sort", DEFAULTS["model_sort"])).strip().lower()
        if sort_order not in {"name", "modified"}:
            sort_order = DEFAULTS["model_sort"]
        params.SetString("ModelSort", sort_order)
        params.SetInt("PushOnExit", int(bool(values.get("push_on_exit", False))))
        params.SetInt(
            "AllowInsecureHTTP",
            int(bool(values.get("allow_insecure_http", False))),
        )
        # Trust is granted for one canonical remote/username pair only.  A
        # decision recorded for the previous target is dropped when the
        # connection changes, so it never leaks to another repository.
        trusted = str(values.get("trusted_repository", "")).strip()
        old_target = canonical_remote_target(
            old.get("server_url", ""), old.get("username", "")
        )
        new_target = canonical_remote_target(server_url, username)
        if old_target != new_target and trusted == old_target:
            trusted = ""
        params.SetString("TrustedRepository", trusted)

        if "token" in values and values["token"] is not None:
            self.credentials.set(username, server_url, str(values["token"]))

        _save_parameters()
        return self.load()

    def clear_token(self) -> None:
        current = self.load()
        self.credentials.clear(current["username"], current["server_url"])
        _save_parameters()


def normalized_server_url(url: str) -> str:
    return _safe_url_for_display(str(url).strip())


def credential_fallback_warning(settings_store: SettingsStore) -> str:
    if settings_store.credentials.using_fallback:
        return settings_store.credentials.status_text
    return ""


def credential_keyring_error(settings_store: SettingsStore) -> str:
    error = settings_store.credentials._keyring_error
    return str(error) if error else ""


__all__ = [
    "CredentialStore",
    "SettingsStore",
    "normalized_server_url",
    "credential_fallback_warning",
    "credential_keyring_error",
]
