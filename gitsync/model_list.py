"""Grouping and ordering of the repository model list."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Sort order for the model list.
NAME_SORT = "name"
MODIFIED_SORT = "modified"

SORT_ORDERS = (NAME_SORT, MODIFIED_SORT)

#: One entry per top-level row: the POSIX folder path ("" for a model in the
#: repository root) and the models it holds, in the requested order.  A folder
#: holds all of its models; a root model holds only itself.  Folders and root
#: models are ordered *among each other*, so the caller can render one flat
#: list of rows.
GroupedModels = List[Tuple[str, List[str]]]


def normalize_sort_order(value: str) -> str:
    """Return a supported sort order, falling back to the name order."""
    order = str(value or "").strip().lower()
    return order if order in SORT_ORDERS else NAME_SORT


def format_timestamp(seconds: Optional[float]) -> str:
    """Format a modification date for the model list column.

    The date alone is deliberate: the column only needs enough to tell models
    apart at a glance, and dropping the time keeps it narrow so the model name
    gets the space instead.
    """
    if not seconds:
        return ""
    try:
        return datetime.fromtimestamp(float(seconds)).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


def newest_time(files: Iterable[str], mtimes: Optional[Dict[str, float]] = None) -> float:
    """Return the most recent modification time among the given models.

    A folder is ordered and dated by the newest file it contains, so this is
    also what the tree shows in the folder's own Modified cell.
    """
    times = mtimes or {}
    return max(
        (float(times.get(str(value), 0.0) or 0.0) for value in files), default=0.0
    )


def _file_key(sort_order: str, mtimes: Dict[str, float]):
    if sort_order == MODIFIED_SORT:

        def key(relative: str):
            # Newest first; the name breaks ties so the order is stable.
            return (-float(mtimes.get(relative, 0.0) or 0.0), relative.casefold())

    else:

        def key(relative: str):
            return (relative.casefold(),)

    return key


def matches_filter(relative: str, text: str) -> bool:
    """True when a model matches a search term.

    The term is matched case-insensitively against the repository path, so it
    finds a model by file name as well as by the folder it lives in.
    """
    needle = str(text or "").strip().casefold()
    if not needle:
        return True
    return needle in str(relative or "").casefold()


def filter_models(models: Iterable[str], text: str) -> List[str]:
    """Return only the models whose path contains the search term."""
    values = [str(value) for value in models if value]
    if not str(text or "").strip():
        return values
    return [value for value in values if matches_filter(value, text)]


def group_models(
    models: Iterable[str],
    mtimes: Optional[Dict[str, float]] = None,
    sort_order: str = NAME_SORT,
) -> GroupedModels:
    """Group models by folder and order every top-level row together.

    Each model in the repository root becomes a row of its own, and each folder
    becomes one row holding all of its models, so the two are ordered *among
    each other* instead of in separate blocks:

    - in the name order a folder is alphabetical with the models, by folder name;
    - in the modified order a folder is placed by the most recent file inside it,
      so the most recently edited row comes first.  A row with no known time
      sorts last.

    Models inside a folder keep the requested order among themselves.
    """
    values = [str(value) for value in models if value]
    times = mtimes or {}
    order = normalize_sort_order(sort_order)
    key = _file_key(order, times)

    rows: List[Tuple[str, List[str]]] = []
    grouped: Dict[str, List[str]] = {}
    for relative in values:
        parts = Path(relative).parts
        folder = str(Path(*parts[:-1])) if len(parts) > 1 else ""
        if folder:
            grouped.setdefault(folder, []).append(relative)
        else:
            # A row of its own, so a root model is ordered alongside the folders.
            rows.append(("", [relative]))
    for folder, files in grouped.items():
        rows.append((folder, sorted(files, key=key)))

    def row_key(row: Tuple[str, List[str]]):
        folder, files = row
        # A folder is labelled by its path, a root model by its own path.
        label = folder or files[0]
        if order == MODIFIED_SORT:
            return (-newest_time(files, times), label.casefold())
        return (label.casefold(),)

    rows.sort(key=row_key)
    return rows


def sorted_models(
    models: Iterable[str],
    mtimes: Optional[Dict[str, float]] = None,
    sort_order: str = NAME_SORT,
) -> List[str]:
    """Return a flat, ordered list of models (folders ignored)."""
    flat: List[str] = []
    for _folder, files in group_models(models, mtimes, sort_order):
        flat.extend(files)
    return flat


__all__ = [
    "MODIFIED_SORT",
    "NAME_SORT",
    "SORT_ORDERS",
    "filter_models",
    "format_timestamp",
    "group_models",
    "matches_filter",
    "newest_time",
    "normalize_sort_order",
    "sorted_models",
]
