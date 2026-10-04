"""A minimal Qt stand-in so GitSync's coordination logic is testable headlessly.

The panel and controller modules import :mod:`gitsync.qt_compat`, which expects
one of FreeCAD's PySide shims.  FreeCAD is not available for the plain
``unittest`` run, so the tests install this tiny stub first.  Only the pieces
that the tested code paths touch are meaningful; everything else returns a
harmless placeholder object.
"""

from __future__ import annotations

import sys
import types


class _Placeholder:
    """Callable object that answers any attribute access."""

    def __init__(self, name: str = "qt") -> None:
        self._name = name

    def __call__(self, *args, **kwargs):
        return _Placeholder("{}()".format(self._name))

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Placeholder("{}.{}".format(self._name, name))

    def __bool__(self) -> bool:
        return True

    def __iter__(self):
        return iter(())

    def __repr__(self) -> str:
        return "<qt-stub {}>".format(self._name)


class _StubMeta(type):
    """Class-level attribute access that yields further stub classes."""

    def __getattr__(cls, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return _StubMeta(name, (_StubBase,), {})

    def __repr__(cls) -> str:
        return "<qt-stub class {}>".format(cls.__name__)


class _StubBase(metaclass=_StubMeta):
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Placeholder(name)

    def __repr__(self) -> str:
        return "<qt-stub {}>".format(type(self).__name__)


def install() -> None:
    """Register the stub as FreeCAD's ``PySide`` shim (idempotent)."""
    if "PySide" in sys.modules and getattr(sys.modules["PySide"], "__gitsync_stub__", False):
        return
    pyside = types.ModuleType("PySide")
    pyside.__gitsync_stub__ = True  # type: ignore[attr-defined]
    pyside.QtCore = _StubMeta("QtCore", (_StubBase,), {})  # type: ignore[attr-defined]
    pyside.QtGui = _StubMeta("QtGui", (_StubBase,), {})  # type: ignore[attr-defined]
    pyside.QtWidgets = _StubMeta("QtWidgets", (_StubBase,), {})  # type: ignore[attr-defined]
    sys.modules["PySide"] = pyside


__all__ = ["install", "Placeholder", "_Placeholder", "_StubBase"]
