"""Deciding which files a delete or rename touches, and whether to allow it.

The actual removal and renaming is Git's job (``GitRepository.remove_paths`` and
``GitRepository.move_paths``), so nothing here mutates the workspace.  What lives
here are the rules for *which* paths are involved and when an operation must be
refused — kept apart from the panel so they can be unit-tested without Qt or
FreeCAD.

Every rule refuses a symbolic link.  The caller has already validated that the
path is inside the workspace; this module re-checks because a link could be
swapped in between the two moments.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from .git_service import normalized_model_filename

__all__ = [
    "WorkspaceFileError",
    "preview_path",
    "removal_paths",
    "rename_target",
]


class WorkspaceFileError(Exception):
    """A rename or delete was refused."""


def preview_path(model: Path) -> Path:
    """Return the preview PNG that GitSync keeps beside a model."""
    return Path(model).with_suffix(".png")


def _check_target(model: Path) -> None:
    model = Path(model)
    if model.is_symlink():
        raise WorkspaceFileError("Refusing to act on a symbolic link.")
    if not model.is_file():
        raise WorkspaceFileError(
            "The selected model no longer exists: {}".format(model.name)
        )


def removal_paths(model: Path) -> List[Path]:
    """Return the paths a delete must take: the model, plus its preview.

    The preview goes with it: an orphaned PNG would otherwise linger in the
    working tree as an untracked file forever.  A preview that does not exist,
    or is itself a link, is left alone.
    """
    model = Path(model)
    _check_target(model)
    paths = [model]
    preview = preview_path(model)
    if preview.is_file() and not preview.is_symlink():
        paths.append(preview)
    return paths


def rename_target(model: Path, new_name: str) -> Tuple[Path, Path]:
    """Return ``(model, target)`` for a rename, or raise if it must not happen.

    The new name goes through the same validation as a new model, so it is a
    single file name ending in ``.FCStd`` with no separators or reserved
    characters.  Nothing is touched on disk here: Git performs the rename.
    """
    model = Path(model)
    _check_target(model)
    filename = normalized_model_filename(new_name)
    if filename == model.name:
        raise WorkspaceFileError("The new name is the same as the current one.")
    target = model.with_name(filename)
    if target.is_symlink() or target.exists():
        raise WorkspaceFileError("{} already exists in the repository.".format(filename))
    # A case-folding file system would treat "Rack.FCStd" and "rack.FCStd" as
    # one file, so the name has to be compared case-insensitively as well.
    try:
        siblings = {child.name.lower() for child in target.parent.iterdir()}
    except OSError as exc:
        raise WorkspaceFileError(
            "Unable to inspect {}: {}".format(target.parent.name, exc)
        ) from exc
    if filename.lower() in siblings:
        raise WorkspaceFileError("{} already exists in the repository.".format(filename))
    return model, target


def rename_pairs(model: Path, target: Path) -> List[Tuple[Path, Path]]:
    """Return the model and preview renames to perform together.

    The preview follows the model so it keeps its picture.  A preview that
    already exists under the new name is skipped rather than overwritten, and one
    that is a link is never touched.
    """
    model, target = Path(model), Path(target)
    pairs = [(model, target)]
    old_preview = preview_path(model)
    new_preview = preview_path(target)
    if (
        old_preview.is_file()
        and not old_preview.is_symlink()
        and not new_preview.exists()
        and not new_preview.is_symlink()
    ):
        pairs.append((old_preview, new_preview))
    return pairs