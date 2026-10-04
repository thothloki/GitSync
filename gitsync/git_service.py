"""Safe, small Git service used by the GitSync FreeCAD workbench.

The workbench deliberately invokes the installed ``git`` executable instead
of embedding a second Git implementation.  Arguments are always passed as a
list (never through a shell), which avoids command-injection problems and
works on all platforms supported by FreeCAD.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit


MODEL_EXTENSIONS = {
    ".fcstd",
    ".step",
    ".stp",
    ".iges",
    ".igs",
    ".stl",
    ".obj",
    ".ply",
    ".brep",
}

# Characters that are rejected in a new file name.  The set covers the
# characters Windows and macOS refuse, plus the path separators that would
# move a document out of the repository root.
FORBIDDEN_NAME_CHARACTERS = set('<>:"\\|?*/')
RESERVED_STEMS = (
    {"con", "prn", "aux", "nul", "clockcon"}
    | {"com{}".format(index) for index in range(1, 10)}
    | {"lpt{}".format(index) for index in range(1, 10)}
)


def normalized_model_filename(value: str) -> str:
    """Validate a user-supplied name for a new FreeCAD document.

    Returns a plain file name ending in ``.FCStd``, which is appended unless the
    name already ends that way.  Dots inside the name are kept, so ``0.test``
    becomes ``0.test.FCStd``.  Path separators, control characters, and names
    other platforms reject are refused, so a new document can only ever be
    created directly inside the workspace.
    """
    name = str(value or "").strip()
    if not name:
        raise GitError("Enter a file name for the new document.")
    if len(name) > 120:
        raise GitError("The file name is longer than 120 characters.")
    if name in {".", ".."} or name.startswith("."):
        raise GitError("The file name cannot start with a dot.")
    if any(character in FORBIDDEN_NAME_CHARACTERS for character in name):
        raise GitError('The file name cannot contain / \\ : * ? " < > | characters.')
    if any(ord(character) < 32 for character in name):
        raise GitError("The file name cannot contain control characters.")
    if name.endswith((" ", ".")):
        raise GitError("The file name cannot end with a space or a dot.")
    # A dot is part of the name, not an extension: "0.test" and "v2.0" are
    # ordinary model names.  Only an explicit .FCStd ending is honoured, so a
    # user who types the suffix anyway does not get "Rack.FCStd.FCStd".
    stem = name[: -len(".FCStd")] if name.lower().endswith(".fcstd") else name
    stem = stem.strip()
    if not stem:
        raise GitError("Enter a file name for the new document.")
    if len(stem) > 100:
        raise GitError("The file name is longer than 100 characters.")
    if stem.split(".", 1)[0].lower() in RESERVED_STEMS:
        raise GitError("'{}' is a reserved name; choose another.".format(stem))
    return "{}.FCStd".format(stem)

LOCAL_EXCLUDE_BLOCK = """\n# GitSync local FreeCAD exclusions\n*.FCStd1\n*.FCBak\n*.FCStd.tmp\n*.FCStd~\n*.tmp\n__pycache__/\n"""


class GitError(RuntimeError):
    """Base class for user-facing GitSync errors."""


class GitNotConfiguredError(GitError):
    """Raised when an operation needs a repository that is not configured."""


class GitCommandError(GitError):
    """A Git command returned a non-zero status."""

    def __init__(
        self,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
        secret: str = "",
    ) -> None:
        self.command = [_redact(str(part), secret) for part in command]
        self.returncode = returncode
        self.stdout = _redact(stdout, secret)
        self.stderr = _redact(stderr, secret)
        detail = (self.stderr or self.stdout or "Git command failed").strip()
        display_command = " ".join(_redact(part, secret) for part in self.command)
        message = "Git command failed ({}): {}".format(display_command, _redact(detail, secret))
        super().__init__(message)


@dataclass
class GitResult:
    """Result of one Git invocation."""

    args: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return self.stdout.strip()


def _redact(value: str, secret: str = "") -> str:
    """Remove a token and URL userinfo from text before displaying it."""
    text = str(value or "")
    if secret:
        text = text.replace(secret, "***")
    # Do not expose credentials even if a user pasted them into the URL.
    text = re.sub(r"(https?://)([^/@\s]+)@", r"\1***@", text, flags=re.IGNORECASE)
    return text


def _safe_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        host = parsed.hostname or ""
        if parsed.port:
            host = "{}:{}".format(host, parsed.port)
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        return url


def canonical_remote_target(url: str, username: str = "") -> str:
    """Return a stable identity for a remote/username pair.

    This is used only for credential lookup and change detection.  It omits
    query/fragment data so credentials can never become part of a keyring
    account name.
    """
    value = str(url or "").strip()
    try:
        parsed = urlsplit(value)
        if parsed.scheme and parsed.netloc:
            scheme = parsed.scheme.lower()
            hostname = (parsed.hostname or "").lower()
            if ":" in hostname and not hostname.startswith("["):
                hostname = "[{}]".format(hostname)
            try:
                port = parsed.port
            except ValueError:
                port = None
            host = hostname
            if port is not None:
                host = "{}:{}".format(host, port)
            path = parsed.path.rstrip("/") or "/"
            value = urlunsplit((scheme, host, path, "", ""))
        else:
            value = value.rstrip("/")
    except Exception:
        value = value.rstrip("/")
    return "{}|{}".format(value, str(username or "").strip())


def validate_remote_url(url: str) -> str:
    """Validate an HTTP(S) clone URL and return its trimmed form."""
    value = str(url or "").strip()
    if not value:
        raise ValueError("A repository URL is required.")
    if any(char.isspace() for char in value):
        raise ValueError("The repository URL cannot contain spaces.")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("GitSync currently supports HTTP and HTTPS repository URLs.")
    if not parsed.netloc:
        raise ValueError("The repository URL must include a server and repository name.")
    if parsed.username or parsed.password:
        raise ValueError("Enter credentials in the username and token fields, not in the URL.")
    if parsed.query or parsed.fragment:
        raise ValueError("Remove query parameters and fragments from the repository URL.")
    return value


def _url_equivalent(left: str, right: str) -> bool:
    return canonical_remote_target(left).split("|", 1)[0] == canonical_remote_target(right).split("|", 1)[0]


def _make_askpass_files(directory: Path, username: str, token: str):
    """Create a cross-platform askpass helper without writing the secret."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        script = directory / "gitsync_askpass.py"
        script.write_text(
            "import os, sys\n"
            "prompt = ' '.join(sys.argv[1:]).lower()\n"
            "value = os.environ.get('GITSYNC_USERNAME', '') if 'username for' in prompt else os.environ.get('GITSYNC_TOKEN', '')\n"
            "print(value)\n",
            encoding="utf-8",
        )
        wrapper = directory / "gitsync_askpass.cmd"
        python = subprocess.list2cmdline([sys.executable, str(script)])
        wrapper.write_text("@echo off\r\n{} \"%~1\"\r\n".format(python), encoding="utf-8")
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return str(wrapper)

    script = directory / "gitsync_askpass.sh"
    script.write_text(
        "#!/bin/sh\n"
        "prompt=\"$1\"\n"
        "case \"$prompt\" in\n"
        "  *[Uu]sername\\ for*|*USERNAME\\ for*) printf '%s\\n' \"$GITSYNC_USERNAME\" ;;\n"
        "  *) printf '%s\\n' \"$GITSYNC_TOKEN\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def _sanitized_git_environment() -> dict:
    """Build a Git environment without repository/config injection variables."""
    env = os.environ.copy()
    blocked_exact = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_EXEC_PATH",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_TEMPLATE_DIR",
        "GIT_ALLOW_PROTOCOL",
        "GIT_SSL_NO_VERIFY",
        "GIT_PROXY_COMMAND",
        "GIT_HTTP_EXTRA_HEADERS",
    }
    for key in list(env):
        if key in blocked_exact or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            env.pop(key, None)
        elif key.startswith("GIT_TRACE") or key.startswith("GIT_CURL_VERBOSE"):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_LITERAL_PATHSPECS"] = "1"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    return env


@contextmanager
def _authentication_environment(username: str, token: str):
    """Yield an environment that can answer Git's HTTP auth prompts."""
    env = _sanitized_git_environment()
    # Never allow Git to block on an invisible terminal prompt.  A configured
    # credential helper can still supply credentials without this variable.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["GCM_INTERACTIVE"] = "Never"
    if not token and not username:
        yield env
        return

    with tempfile.TemporaryDirectory(prefix="gitsync-askpass-") as temp_name:
        temp_dir = Path(temp_name)
        askpass = _make_askpass_files(temp_dir, username, token)
        env["GIT_ASKPASS"] = askpass
        env["GITSYNC_USERNAME"] = username or ""
        env["GITSYNC_TOKEN"] = token or ""
        yield env


class GitRepository:
    """Operations for one local Git working tree."""

    def __init__(
        self,
        workspace: str,
        remote_url: str = "",
        username: str = "",
        token: str = "",
        git_executable: Optional[str] = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()
        self.remote_url = remote_url or ""
        self.username = username or ""
        self.token = token or ""
        configured_git = os.environ.get("GITSYNC_GIT", "")
        if git_executable:
            self.git_executable = git_executable
        elif configured_git and Path(configured_git).name.lower() in {"git", "git.exe"}:
            self.git_executable = configured_git
        else:
            self.git_executable = shutil.which("git") or "git"
        self._root: Optional[Path] = None

    @property
    def root(self) -> Path:
        return self._root or self.workspace.resolve()

    def _display_error(self, result: GitResult) -> GitCommandError:
        return GitCommandError(
            result.args,
            result.returncode,
            result.stdout,
            result.stderr,
            self.token,
        )

    def run(
        self,
        args: Sequence[str],
        cwd: Optional[Path] = None,
        check: bool = True,
        timeout: int = 180,
    ) -> GitResult:
        """Run Git without a shell and return a structured result."""
        command = [self.git_executable, *[str(arg) for arg in args]]
        workdir = Path(cwd) if cwd is not None else self.root
        try:
            environment = _sanitized_git_environment()
            completed = subprocess.run(
                command,
                cwd=str(workdir),
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            if not workdir.exists():
                raise GitError("The Git working directory no longer exists: {}".format(workdir)) from exc
            raise GitError("Git executable not found. Install Git and restart FreeCAD.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError("Git operation timed out after {} seconds.".format(timeout)) from exc
        except OSError as exc:
            raise GitError("Unable to start Git: {}".format(exc)) from exc

        result = GitResult(command, completed.returncode, completed.stdout or "", completed.stderr or "")
        if check and not result.ok:
            raise self._display_error(result)
        return result

    def run_with_auth(
        self,
        args: Sequence[str],
        cwd: Optional[Path] = None,
        check: bool = True,
        timeout: int = 180,
    ) -> GitResult:
        """Run Git with the configured HTTP credentials when necessary."""
        command = [self.git_executable, *[str(arg) for arg in args]]
        workdir = Path(cwd) if cwd is not None else self.root
        try:
            with _authentication_environment(self.username, self.token) as env:
                if self.remote_url.lower().startswith(("http://", "https://")):
                    env["GIT_ALLOW_PROTOCOL"] = "http:https"
                completed = subprocess.run(
                    command,
                    cwd=str(workdir),
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    stdin=subprocess.DEVNULL,
                    timeout=timeout,
                    check=False,
                )
        except FileNotFoundError as exc:
            if not workdir.exists():
                raise GitError("The Git working directory no longer exists: {}".format(workdir)) from exc
            raise GitError("Git executable not found. Install Git and restart FreeCAD.") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError("Git operation timed out after {} seconds.".format(timeout)) from exc
        except OSError as exc:
            raise GitError("Unable to start Git: {}".format(exc)) from exc

        result = GitResult(command, completed.returncode, completed.stdout or "", completed.stderr or "")
        if check and not result.ok:
            raise self._display_error(result)
        return result

    def is_repository(self) -> bool:
        if not self.workspace.exists() or not self.workspace.is_dir():
            return False
        result = self.run(["rev-parse", "--show-toplevel"], check=False, timeout=15)
        if not result.ok:
            return False
        top = result.output.strip()
        if not top:
            return False
        discovered_root = Path(top).expanduser().resolve()
        workspace_root = self.workspace.resolve()
        if discovered_root != workspace_root:
            # A selected subdirectory of another checkout is not a valid
            # GitSync workspace.  Do not silently operate on its parent.
            self._root = None
            return False
        self._root = discovered_root
        return True

    def require_repository(self) -> None:
        if not self.is_repository():
            raise GitNotConfiguredError("The selected workspace is not a Git repository.")
        self._ensure_local_excludes()

    def _configured_remote(self) -> str:
        result = self.run(["remote", "get-url", "origin"], check=False, timeout=15)
        return result.output.strip() if result.ok else ""

    def _remote_url(self, remote: str, push: bool = False) -> str:
        args = ["remote", "get-url"]
        if push:
            args.append("--push")
        args.append(remote)
        result = self.run(args, check=False, timeout=15)
        return result.output.strip() if result.ok else ""

    def _default_remote(self) -> str:
        branch = self.current_branch()
        if branch and branch != "HEAD (detached)":
            result = self.run(
                ["config", "--get", "branch.{}.remote".format(branch)],
                check=False,
                timeout=15,
            )
            if result.ok and result.output.strip():
                return result.output.strip()
        return "origin"

    def upstream_remote(self) -> str:
        branch = self.current_branch()
        if branch and branch != "HEAD (detached)":
            result = self.run(
                ["config", "--get", "branch.{}.remote".format(branch)],
                check=False,
                timeout=15,
            )
            if result.ok and result.output.strip():
                return result.output.strip()
        upstream = self.upstream()
        return upstream.split("/", 1)[0] if "/" in upstream else ""

    def _validate_network_remote(self, remote: str) -> None:
        if not self.remote_url:
            raise GitError("Configure an HTTP(S) repository URL before using network operations.")
        remote = str(remote or "").strip()
        if not remote:
            raise GitError("This branch has no configured remote.")
        fetch_url = self._remote_url(remote)
        push_url = self._remote_url(remote, push=True)
        if not fetch_url:
            raise GitError("The Git remote '{}' has no fetch URL.".format(remote))
        actual_urls = [fetch_url]
        if push_url:
            actual_urls.append(push_url)
        # A configured HTTP(S) target is a trust boundary.  Do not send
        # credentials to a manually changed SSH/local/helper remote.
        enforce_http = self.remote_url.lower().startswith(("http://", "https://")) or bool(self.username or self.token)
        if enforce_http:
            for actual in actual_urls:
                try:
                    validate_remote_url(actual)
                except ValueError as exc:
                    raise GitError("Remote '{}' is not an approved HTTP(S) URL: {}".format(remote, exc)) from exc
                if self.remote_url and not _url_equivalent(actual, self.remote_url):
                    raise GitError(
                        "Remote '{}' points to a different URL than the configured repository.".format(remote)
                    )

    def connect_or_clone(self, remote_url: str) -> Path:
        """Clone into the workspace or validate an existing checkout."""
        remote_url = validate_remote_url(remote_url)
        self.remote_url = remote_url
        workspace = self.workspace.expanduser()

        if workspace.exists() and not workspace.is_dir():
            raise GitError("The local workspace is not a directory: {}".format(workspace))

        if self.is_repository():
            # Do not silently operate on a parent repository when the user
            # selected a nested directory during setup.
            if self.root != workspace.resolve():
                raise GitError(
                    "The selected directory is inside a different Git repository ({})".format(self.root)
                )
            self.workspace = self.root
            existing_remote = self._configured_remote()
            if existing_remote and not _url_equivalent(existing_remote, remote_url):
                raise GitError(
                    "The workspace is connected to a different remote.\n"
                    "Configured remote: {}".format(_safe_url(existing_remote))
                )
            if not existing_remote:
                self.run(["remote", "add", "origin", remote_url])
            self._validate_network_remote("origin")
            self._ensure_local_excludes()
            return self.root

        if workspace.exists() and any(workspace.iterdir()):
            raise GitError(
                "The selected directory is not empty and is not a Git repository. "
                "Choose an empty directory or an existing checkout."
            )

        workspace.parent.mkdir(parents=True, exist_ok=True)
        # ``--`` prevents a URL beginning with a dash from being interpreted as
        # an option.  The destination is an absolute path for predictable UI
        # behavior on all platforms.
        destination = workspace.resolve()
        self.run_with_auth(["clone", "--", remote_url, str(destination)], cwd=destination.parent, timeout=600)
        self._root = destination
        self._validate_network_remote("origin")
        self._ensure_local_excludes()
        return self.root

    def _ensure_local_excludes(self) -> None:
        """Add FreeCAD backup patterns to .git/info/exclude, never to tracked files."""
        git_path_result = self.run(["rev-parse", "--git-path", "info/exclude"], timeout=15)
        if not git_path_result.ok:
            return
        exclude_file = Path(git_path_result.output.strip())
        if not exclude_file.is_absolute():
            exclude_file = self.root / exclude_file
        try:
            # Do not follow a repository-controlled symlink when updating the
            # local exclude file.
            current = self.root
            raw_exclude = Path(os.path.abspath(str(exclude_file)))
            relative_parts = raw_exclude.relative_to(self.root.resolve()).parts
            for part in relative_parts:
                current = current / part
                if current.is_symlink():
                    return
            if exclude_file.is_symlink():
                return
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
            marker = "# GitSync local FreeCAD exclusions"
            if marker not in existing:
                with exclude_file.open("a", encoding="utf-8") as handle:
                    handle.write(LOCAL_EXCLUDE_BLOCK)
        except (OSError, UnicodeError, ValueError):
            # Excludes are a convenience; failing to write them must not make
            # a valid repository unusable.
            pass

    def update_credentials(
        self,
        username: Optional[str] = None,
        token: Optional[str] = None,
        remote_url: Optional[str] = None,
    ) -> None:
        if remote_url:
            self.remote_url = remote_url
        if username is not None:
            self.username = username
        if token is not None:
            self.token = token

    def contains_path(self, path: str) -> bool:
        try:
            candidate = Path(path).expanduser().resolve()
            candidate.relative_to(self.root.resolve())
            return True
        except (OSError, ValueError):
            return False

    def relative_path(self, path: str) -> str:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return candidate.resolve().relative_to(self.root.resolve()).as_posix()

    def _normalize_paths(self, paths: Iterable[str]) -> List[str]:
        normalized: List[str] = []
        for value in paths:
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                path = self.root / path
            try:
                rel = path.resolve().relative_to(self.root.resolve())
            except ValueError as exc:
                raise GitError("Path is outside the Git workspace: {}".format(value)) from exc
            text = rel.as_posix()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    def current_branch(self) -> str:
        result = self.run(["branch", "--show-current"], check=False, timeout=15)
        branch = result.output.strip()
        if branch:
            return branch
        symbolic = self.run(["symbolic-ref", "--short", "-q", "HEAD"], check=False, timeout=15)
        return symbolic.output.strip() or "HEAD (detached)"

    def upstream(self) -> str:
        result = self.run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], check=False, timeout=15)
        return result.output.strip() if result.ok else ""

    def _parse_status(self, raw: str) -> List[Dict[str, object]]:
        entries: List[Dict[str, object]] = []
        records = raw.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 3:
                continue
            code = record[:2]
            path = record[3:]
            related_paths = [path]
            # Rename/copy records in porcelain -z are followed by the source
            # path. Keep both paths so selecting a rename stages the deletion
            # of the old path as well as the new path.
            if code[0] in {"R", "C"} or code[1] in {"R", "C"}:
                if index < len(records):
                    source = records[index]
                    index += 1
                    if source:
                        related_paths.append(source)
            staged = code[0] not in {" ", "?", "!"}
            worktree = code[1] not in {" ", "?", "!"}
            entries.append(
                {
                    "path": path,
                    "related_paths": related_paths,
                    "status": code,
                    "staged": staged,
                    "worktree": worktree,
                    "untracked": code == "??",
                }
            )
        return entries

    def status(self) -> Dict[str, object]:
        self.require_repository()
        entries_result = self.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            timeout=30,
        )
        entries = self._parse_status(entries_result.stdout)
        branch = self.current_branch()
        upstream_name = self.upstream()
        ahead = 0
        behind = 0
        if upstream_name:
            comparison = self.run(
                ["rev-list", "--left-right", "--count", "HEAD...{}".format(upstream_name)],
                check=False,
                timeout=30,
            )
            if comparison.ok:
                values = comparison.output.split()
                if len(values) >= 2:
                    try:
                        ahead, behind = int(values[0]), int(values[1])
                    except ValueError:
                        pass
        last_commit = ""
        log_result = self.run(["log", "-1", "--pretty=format:%h %s"], check=False, timeout=15)
        if log_result.ok:
            last_commit = log_result.output.strip()
        return {
            "root": str(self.root),
            "remote": _safe_url(self._remote_url(self._default_remote())),
            "branch": branch,
            "upstream": upstream_name,
            "ahead": ahead,
            "behind": behind,
            "entries": entries,
            "clean": not entries,
            "last_commit": last_commit,
        }

    def safe_model_path(self, path: str) -> Path:
        """Resolve a model path while rejecting symlink escapes.

        Git paths are untrusted repository data.  Rejecting symlinks in every
        component prevents a tracked link from making the preview/open code
        access an arbitrary file outside the checkout.
        """
        root = self.root.resolve()
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            raw_candidate = Path(os.path.abspath(str(candidate)))
            raw_relative = raw_candidate.relative_to(root)
        except ValueError as exc:
            raise GitError("Model path is outside the Git workspace: {}".format(path)) from exc

        current = root
        for part in raw_relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    raise GitError("Symbolic links are not allowed in GitSync model paths: {}".format(path))
            except OSError as exc:
                raise GitError("Unable to inspect model path: {}".format(path)) from exc
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise GitError("Model path is not a regular file in the workspace: {}".format(path)) from exc
        if not resolved.is_file():
            raise GitError("Model path is not a regular file: {}".format(path))
        return resolved

    def model_files(self) -> List[str]:
        self.require_repository()
        result = self.run(
            ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=False,
            timeout=30,
        )
        if not result.ok:
            return []
        files: List[str] = []
        for value in result.stdout.split("\0"):
            if not value:
                continue
            if Path(value).suffix.lower() not in MODEL_EXTENSIONS:
                continue
            try:
                self.safe_model_path(value)
            except GitError:
                continue
            files.append(value)
        return sorted(set(files), key=lambda item: item.casefold())

    def model_mtimes(self, models: Sequence[str]) -> Dict[str, float]:
        """Return the workspace modification time of each model.

        The listing runs in a background thread, so the stat calls the model
        browser needs for its sort order happen here instead of on the GUI
        thread.  A missing or unreadable file maps to ``0.0``.
        """
        values: Dict[str, float] = {}
        root = self.root
        for relative in models:
            try:
                values[str(relative)] = (root / str(relative)).stat().st_mtime
            except OSError:
                values[str(relative)] = 0.0
        return values

    def snapshot(self) -> Dict[str, object]:
        result = self.status()
        result["models"] = self.model_files()
        result["model_mtimes"] = self.model_mtimes(result["models"])
        return result

    def commit_paths(
        self,
        paths: Sequence[str],
        message: str,
        force_add: bool = False,
    ) -> Dict[str, object]:
        self.require_repository()
        message = str(message or "").strip()
        if not message:
            raise GitError("Enter a commit message.")
        normalized = self._normalize_paths(paths)
        add_paths: List[str] = []
        for relative in normalized:
            if (self.root / relative).exists():
                add_paths.append(relative)
                continue
            # A source path in an already-staged rename no longer exists in
            # the index.  Git accepts it as a commit pathspec, but rejects it
            # as an ``add`` pathspec, so only add paths that still exist or
            # remain tracked in the index.
            tracked = self.run(
                ["ls-files", "--error-unmatch", "--", relative],
                check=False,
                timeout=15,
            )
            if tracked.ok:
                add_paths.append(relative)
        if add_paths:
            # Preview PNGs are force-added in their own ``add`` invocation so a
            # repository that ignores *.png still tracks its previews, without
            # overriding ignore rules for the model files themselves.
            preview_paths = [
                path for path in add_paths if Path(path).suffix.lower() == ".png"
            ]
            regular_paths = [path for path in add_paths if path not in preview_paths]
            if regular_paths:
                add_command = ["add", "-A"]
                if force_add:
                    add_command.append("-f")
                self.run([*add_command, "--", *regular_paths], timeout=60)
            if preview_paths:
                self.run(["add", "-A", "-f", "--", *preview_paths], timeout=60)
        elif not normalized:
            self.run(["add", "-A"], timeout=60)
        staged_args = ["diff", "--cached", "--quiet"]
        if normalized:
            staged_args.extend(["--", *normalized])
        staged = self.run(staged_args, check=False, timeout=30)
        if staged.returncode == 0:
            return {"created": False, "message": message, "paths": normalized}
        if staged.returncode != 1:
            raise self._display_error(staged)
        if normalized:
            # --only keeps unrelated changes that were already staged by the
            # user out of an automatic or selected-file commit.
            result = self.run(["commit", "--only", "-m", message, "--", *normalized], timeout=120)
        else:
            result = self.run(["commit", "-m", message], timeout=120)
        return {"created": True, "message": message, "paths": normalized, "output": result.output}

    def _addable_paths(self, candidates: Sequence[str]) -> List[str]:
        """Return the paths Git can accept in an ``add`` pathspec.

        ``git add`` fails when a pathspec matches neither an existing file nor
        an index entry, which happens for the preview of a model that was
        deleted before it was ever rendered.
        """
        addable: List[str] = []
        missing: List[str] = []
        for value in candidates:
            path = self.root / value
            if path.exists() and not path.is_symlink():
                addable.append(value)
            else:
                missing.append(value)
        if missing:
            tracked = self.run(["ls-files", "-z", "--", *missing], check=False, timeout=30)
            if tracked.ok:
                names = {value for value in tracked.stdout.split("\0") if value}
                addable.extend(value for value in missing if value in names)
        return addable

    def _tracked_paths(self, paths: Sequence[str]) -> set:
        """Return the subset of ``paths`` that the index still tracks."""
        if not paths:
            return set()
        listed = self.run(["ls-files", "-z", "--", *paths], check=False, timeout=30)
        if not listed.ok:
            return set()
        return {value for value in listed.stdout.split("\0") if value}

    def _commit_now(self, message: str, paths: Sequence[str]) -> Dict[str, object]:
        """Commit exactly the given paths and nothing else already staged."""
        if not paths:
            return {"created": False, "message": message, "paths": [], "output": ""}
        staged = self.run(
            ["diff", "--cached", "--quiet", "--", *paths], check=False, timeout=30
        )
        if staged.returncode == 0:
            return {"created": False, "message": message, "paths": list(paths), "output": ""}
        if staged.returncode != 1:
            raise self._display_error(staged)
        # --only keeps unrelated changes the user had already staged out of it,
        # the same reason commit_paths uses it.
        result = self.run(["commit", "--only", "-m", message, "--", *paths], timeout=120)
        return {
            "created": True,
            "message": message,
            "paths": list(paths),
            "output": result.output,
        }

    def remove_paths(self, paths: Sequence[str], message: str) -> Dict[str, object]:
        """Delete paths with ``git rm`` and commit the removal.

        ``git rm`` is deliberately **not** forced.  Without ``--force`` Git
        refuses a file whose content differs from the index, which is the last
        line of defence against discarding edits that were never committed.
        An untracked file has nothing for Git to record, so it is simply
        removed from disk.
        """
        self.require_repository()
        message = str(message or "").strip()
        if not message:
            raise GitError("Enter a commit message.")
        normalized = self._normalize_paths(paths)
        if not normalized:
            raise GitError("There is nothing to remove.")
        tracked = self._tracked_paths(normalized)
        removed: List[str] = []
        unlinked: List[str] = []
        for relative in normalized:
            path = self.root / relative
            present = path.exists() and not path.is_symlink()
            if relative in tracked and present:
                self.run(["rm", "--quiet", "--", relative], timeout=60)
                removed.append(relative)
            elif relative in tracked:
                # Already gone from disk; stage the deletion Git still records.
                self.run(["add", "-A", "--", relative], timeout=60)
                removed.append(relative)
            elif present:
                try:
                    path.unlink()
                except OSError as exc:
                    raise GitError(
                        "Unable to delete {}: {}".format(path.name, exc)
                    ) from exc
                unlinked.append(relative)
        result = self._commit_now(message, removed)
        result["removed"] = removed
        result["unlinked"] = unlinked
        return result

    def move_paths(
        self, moves: Sequence[Tuple[str, str]], message: str
    ) -> Dict[str, object]:
        """Rename paths with ``git mv`` and commit the rename.

        ``git mv`` is the atomic form of the ``git rm`` of the old name plus the
        add of the new one, so the file is never momentarily untracked.  A
        plain ``git rm`` first cannot work: it deletes the file, leaving nothing
        to rename.  All moves land in a single commit.
        """
        self.require_repository()
        message = str(message or "").strip()
        if not message:
            raise GitError("Enter a commit message.")
        pairs = [(self._normalize_paths([old])[0], self._normalize_paths([new])[0])
                 for old, new in moves]
        tracked = self._tracked_paths([old for old, _new in pairs])
        commit_paths: List[str] = []

        def remember(*values: str) -> None:
            for value in values:
                if value and value not in commit_paths:
                    commit_paths.append(value)

        for old, new in pairs:
            if old == new:
                continue
            if old in tracked:
                self.run(["mv", "--", old, new], timeout=60)
                # A tracked rename needs both names so Git records the rename
                # rather than a bare addition.
                remember(old, new)
            else:
                # Untracked: rename on disk, then record the new name.  The old
                # name was never known to Git, so passing it as a commit
                # pathspec would fail with "did not match any file(s)".
                source = self.root / old
                target = self.root / new
                try:
                    source.rename(target)
                except OSError as exc:
                    raise GitError(
                        "Unable to rename {}: {}".format(source.name, exc)
                    ) from exc
                self.run(["add", "-A", "--", new], timeout=60)
                remember(new)
        result = self._commit_now(message, commit_paths)
        result["moved"] = [[old, new] for old, new in pairs if old != new]
        return result

    def _stale_preview_paths(self) -> List[str]:
        """Return preview PNGs whose model was deleted outside GitSync.

        GitSync's own Delete removes the preview as well, so anything found here
        belongs to a model that disappeared from the filesystem some other way.
        Nothing else would ever remove it, and it would sit in the repository as
        a tracked file with no model behind it.
        """
        listed = self.run(["ls-files", "-z", "--deleted"], check=False, timeout=30)
        if not listed.ok:
            return []
        stale: List[str] = []
        for value in listed.stdout.split("\0"):
            if not value:
                continue
            if Path(value).suffix.lower() not in MODEL_EXTENSIONS:
                continue
            preview = Path(value).with_suffix(".png").as_posix()
            path = self.root / preview
            if path.exists() and not path.is_symlink():
                stale.append(preview)
        return stale

    def _drop_stale_previews(self) -> List[str]:
        """Remove the previews of models deleted outside GitSync.

        A plain unlink, deliberately not ``git rm``: a preview regenerated since
        the last commit differs from the index, and ``git rm`` would refuse it.
        The removal is then picked up by the ``add -A`` the caller runs.
        """
        dropped: List[str] = []
        for relative in self._stale_preview_paths():
            try:
                (self.root / relative).unlink()
            except OSError:
                continue
            dropped.append(relative)
        return dropped

    def commit_all(self, message: str) -> Dict[str, object]:
        self.require_repository()
        message = str(message or "").strip()
        if not message:
            raise GitError("Enter a commit message.")
        dropped_previews = self._drop_stale_previews()
        self.run(["add", "-A"], timeout=60)
        # Keep preview PNGs tracked even when the repository ignores *.png.
        preview_paths = self._addable_paths(
            [Path(model).with_suffix(".png").as_posix() for model in self.model_files()]
        )
        if preview_paths:
            self.run(["add", "-A", "-f", "--", *preview_paths], timeout=60)
        staged = self.run(["diff", "--cached", "--quiet"], check=False, timeout=30)
        if staged.returncode == 0:
            return {"created": False, "message": message, "paths": [],
                    "dropped_previews": dropped_previews}
        if staged.returncode != 1:
            raise self._display_error(staged)
        result = self.run(["commit", "-m", message], timeout=120)
        return {"created": True, "message": message, "paths": [],
                "dropped_previews": dropped_previews, "output": result.output}

    def fetch(self, timeout: int = 120) -> GitResult:
        self.require_repository()
        remote = self._default_remote()
        self._validate_network_remote(remote)
        return self.run_with_auth(["fetch", "--prune", remote], timeout=timeout)

    def pull(self) -> GitResult:
        self.require_repository()
        if not self.upstream():
            raise GitError("The current branch has no upstream branch. Configure it in Git first.")
        self._validate_network_remote(self.upstream_remote())
        return self.run_with_auth(["pull", "--ff-only"], timeout=600)

    def push(self, timeout: int = 600) -> GitResult:
        self.require_repository()
        head = self.run(["rev-parse", "--verify", "HEAD"], check=False, timeout=15)
        if not head.ok:
            return GitResult(["push"], 0, "No commits to push.", "")
        branch = self.current_branch()
        if not self.upstream():
            remote = self._default_remote()
            self._validate_network_remote(remote)
            if branch == "HEAD (detached)":
                raise GitError("Cannot push a detached HEAD.")
            return self.run_with_auth(["push", "-u", remote, branch], timeout=timeout)
        self._validate_network_remote(self.upstream_remote())
        return self.run_with_auth(["push"], timeout=timeout)

    def set_upstream(self) -> GitResult:
        self.require_repository()
        branch = self.current_branch()
        if not branch or branch == "HEAD (detached)":
            raise GitError("Cannot configure an upstream for a detached HEAD.")
        remote = self._default_remote()
        self._validate_network_remote(remote)
        return self.run(["branch", "--set-upstream-to={}/{}".format(remote, branch), branch], timeout=60)

    def merge_remote(self) -> GitResult:
        self.require_repository()
        upstream_ref = self.upstream()
        if not upstream_ref:
            raise GitError("The current branch has no upstream branch to merge.")
        self._validate_network_remote(self.upstream_remote())
        return self.run(["merge", "--no-edit", upstream_ref], timeout=600)

    def rebase_remote(self) -> GitResult:
        self.require_repository()
        upstream_ref = self.upstream()
        if not upstream_ref:
            raise GitError("The current branch has no upstream branch to rebase onto.")
        self._validate_network_remote(self.upstream_remote())
        return self.run_with_auth(["rebase", upstream_ref], timeout=600)

    def changed_files(self, range_spec: str) -> List[str]:
        result = self.run(["diff", "--name-status", range_spec], check=False, timeout=30)
        if not result.ok:
            return []
        return [line.strip() for line in result.output.splitlines() if line.strip()]

    def log_range(self, range_spec: str, limit: int = 20) -> List[str]:
        result = self.run(
            ["log", "--oneline", "--decorate", "--max-count={}".format(int(limit)), range_spec],
            check=False,
            timeout=30,
        )
        return [line for line in result.output.splitlines() if line.strip()] if result.ok else []


__all__ = [
    "GitError",
    "GitNotConfiguredError",
    "GitCommandError",
    "GitResult",
    "GitRepository",
    "MODEL_EXTENSIONS",
    "validate_remote_url",
    "canonical_remote_target",
]
