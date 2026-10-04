"""Launching the operating system's own file browser.

Kept apart from the panel so the platform mapping can be unit-tested without Qt
or FreeCAD.  Nothing here ever reaches the model data: the caller hands over an
already validated path and this module only turns it into an argument vector.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Sequence

__all__ = ["reveal_command", "reveal_in_file_manager", "select_command"]

#: How long the FileManager1 request may take.  Dolphin and friends answer as
#: soon as they have raised the window, so this only covers a stalled bus.
_DBUS_TIMEOUT = 5


def _dbus_available() -> bool:
    """True when a session bus and dbus-send are both usable."""
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")) and bool(
        shutil.which("dbus-send")
    )


def select_command(path: Path, platform: str = sys.platform) -> Optional[List[str]]:
    """Return a command that reveals *and selects* ``path``, or ``None``.

    ``None`` means the platform offers no portable way to preselect a file, and
    the caller should fall back to opening the containing folder.

    On Linux this is the standard ``org.freedesktop.FileManager1`` D-Bus call,
    implemented by Dolphin, Nautilus, Nemo, Thunar and PCManFM.  It needs a
    session bus, so it is only offered when one is actually there.
    """
    if platform.startswith("win"):
        # explorer takes the path glued to the switch with a comma and no
        # space, as a single argument.  The path is never shell-quoted here:
        # it is passed through argv, so quoting it would break the argument.
        return ["explorer", "/select,{}".format(path)]
    if platform == "darwin":
        return ["open", "-R", str(path)]
    if not _dbus_available():
        return None
    return [
        "dbus-send",
        "--session",
        "--dest=org.freedesktop.FileManager1",
        "--type=method_call",
        "--print-reply=literal",
        "/org/freedesktop/FileManager1",
        "org.freedesktop.FileManager1.ShowItems",
        "array:string:{}".format(Path(path).as_uri()),
        # An empty startup id, typed.  dbus-send rejects a bare `""` as
        # "badly formed", and a bare word is not accepted as a string either.
        "string:",
    ]


def reveal_command(path: Path, platform: str = sys.platform) -> List[str]:
    """Return the command that opens ``path``'s folder in the file browser.

    The fallback for a platform that cannot preselect: Linux's ``xdg-open``
    opens the containing folder.  Passing the file itself would be wrong there,
    because it would launch whatever application handles ``.FCStd``.
    """
    if platform.startswith("win"):
        return ["explorer", str(path.parent)]
    if platform == "darwin":
        return ["open", str(path.parent)]
    return ["xdg-open", str(path.parent)]


def reveal_in_file_manager(
    path: Path,
    platform: str = sys.platform,
    runner: Optional[Callable[[Sequence[str]], None]] = None,
) -> str:
    """Show ``path`` in the system file browser, selecting it where possible.

    Returns the command that was used.  When nothing can select the file, the
    containing folder is opened instead, so the model is always reachable.

    ``runner`` receives each command it is asked to try; by default commands
    are started detached so the panel never blocks.  It exists for the tests,
    which must not launch anything.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(
            "The selected model no longer exists: {}".format(target)
        )

    select = select_command(target, platform)
    if select is not None:
        if runner is not None:
            runner(select)
            return " ".join(select)
        try:
            # --print-reply makes dbus-send wait, so a missing or failing
            # FileManager1 shows up as a non-zero exit and we can fall back.
            completed = subprocess.run(
                select,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=_DBUS_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            return " ".join(select)

    command = reveal_command(target, platform)
    if runner is None:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
    else:
        runner(command)
    return " ".join(command)