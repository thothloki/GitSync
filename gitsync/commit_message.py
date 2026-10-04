"""Build the commit messages used by GitSync's automatic commits."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

TIMESTAMP_MODE = "timestamp"
CUSTOM_MODE = "custom"

#: Placeholders a custom commit message may use.
PLACEHOLDERS = ("timestamp", "date", "time", "filename", "name", "count")


def timestamp(moment: Optional[datetime] = None) -> str:
    """Return the local date and time used in automatic commit messages."""
    return (moment or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _replacements(relative_paths: Sequence[str], moment: Optional[datetime]) -> Dict[str, str]:
    moment = moment or datetime.now()
    paths = [str(value) for value in relative_paths]
    if len(paths) == 1:
        filename = Path(paths[0]).name
    elif paths:
        filename = "{} documents".format(len(paths))
    else:
        filename = ""
    name = Path(filename).stem if filename else ""
    return {
        "timestamp": moment.strftime("%Y-%m-%d %H:%M:%S"),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M:%S"),
        "filename": filename,
        "name": name,
        "count": str(len(paths)),
    }


def default_message(prefix: str, relative_paths: Sequence[str], moment: Optional[datetime] = None) -> str:
    """Return the timestamp message used when no custom message is set."""
    values = _replacements(relative_paths, moment)
    if not prefix:
        return values["timestamp"]
    detail = values["filename"] or values["timestamp"]
    if not values["filename"]:
        return "{} {}".format(prefix, values["timestamp"])
    return "{} {} - {}".format(prefix, values["timestamp"], detail)


def _apply_template(template: str, values: Dict[str, str]) -> Optional[str]:
    try:
        return template.format(**values)
    except KeyError:
        # An unknown placeholder is kept verbatim instead of failing the save.
        text = template
        for key, value in values.items():
            text = text.replace("{" + key + "}", value)
        return text
    except (IndexError, ValueError):
        # A structurally broken template cannot be repaired.
        return None


def build_commit_message(
    settings: Dict[str, Any],
    relative_paths: Sequence[str],
    prefix: str = "Auto-save",
    moment: Optional[datetime] = None,
) -> str:
    """Return the commit message for an automatic commit.

    ``settings`` chooses between a plain date/time stamp (``timestamp`` mode,
    the default) and a user-supplied ``custom_commit_message``.  The custom
    message may use the placeholders in :data:`PLACEHOLDERS`; if it is empty or
    cannot be formatted, the timestamp message is used instead.
    """
    fallback = default_message(prefix, relative_paths, moment)
    mode = str(settings.get("auto_commit_message", TIMESTAMP_MODE) or TIMESTAMP_MODE).lower()
    if mode != CUSTOM_MODE:
        return fallback
    template = str(settings.get("custom_commit_message", "") or "").strip()
    if not template:
        return fallback
    message = _apply_template(template, _replacements(relative_paths, moment))
    if message is None:
        return fallback
    message = message.strip()
    return message or fallback


__all__ = [
    "CUSTOM_MODE",
    "PLACEHOLDERS",
    "TIMESTAMP_MODE",
    "build_commit_message",
    "default_message",
    "timestamp",
]
