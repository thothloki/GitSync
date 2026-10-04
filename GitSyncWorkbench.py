"""FreeCAD workbench registration for GitSync."""

from __future__ import annotations

import os
import sys

import FreeCAD as App
import FreeCADGui as Gui
import __main__

# FreeCAD exposes every Mod directory on sys.path.  Put this addon first so
# generic helper names such as ``configuration`` cannot resolve to another
# installed workbench's module.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
while _THIS_DIR in sys.path:
    sys.path.remove(_THIS_DIR)
sys.path.insert(0, _THIS_DIR)

from gitsync.qt_compat import QtCore, QtWidgets, dock_area

try:
    _WorkbenchBase = Gui.Workbench
except AttributeError:  # FreeCAD injects the base class into __main__ in some builds.
    _WorkbenchBase = getattr(__main__, "Workbench", None)
if _WorkbenchBase is None:
    raise ImportError("FreeCAD's Python Workbench base class is unavailable.")


_ACTIVE_PANEL = None
_RUNTIME_CONTEXT = None
_RUNTIME_DOCK = None
_RUNTIME_CONTROLLER = None
_RUNTIME_BOOTSTRAP_PENDING = False


def _panel():
    return _ACTIVE_PANEL


def initialize_runtime() -> None:
    """Create the panel and lifecycle hooks once the GUI event loop starts."""
    global _ACTIVE_PANEL
    global _RUNTIME_CONTEXT
    global _RUNTIME_DOCK
    global _RUNTIME_CONTROLLER
    global _RUNTIME_BOOTSTRAP_PENDING

    if _RUNTIME_CONTEXT is not None and _RUNTIME_DOCK is not None:
        return
    if _RUNTIME_CONTEXT is not None:
        shutdown_runtime()

    # InitGui.py can run slightly before FreeCAD has finished constructing its
    # main window.  Retry on the next event-loop turn instead of failing during
    # startup.
    try:
        main_window = Gui.getMainWindow()
    except Exception:
        main_window = None
    if main_window is None:
        if not _RUNTIME_BOOTSTRAP_PENDING:
            _RUNTIME_BOOTSTRAP_PENDING = True
            QtCore.QTimer.singleShot(100, _bootstrap_runtime)
        return

    from gitsync.app_context import GitSyncContext
    from gitsync.sync_events import SyncController
    from gitsync.sync_panel import SyncPanel

    context = GitSyncContext()
    panel = SyncPanel(context)
    dock = QtWidgets.QDockWidget("GitSync", main_window)
    dock.setObjectName("GitSyncDock")
    dock.setWidget(panel)
    dock.setAllowedAreas(
        dock_area("LeftDockWidgetArea")
        | dock_area("RightDockWidgetArea")
        | dock_area("BottomDockWidgetArea")
    )
    main_window.addDockWidget(dock_area("RightDockWidgetArea"), dock)
    dock.show()

    controller = SyncController(context, panel)
    context.controller = controller

    _RUNTIME_CONTEXT = context
    _RUNTIME_DOCK = dock
    _RUNTIME_CONTROLLER = controller
    _ACTIVE_PANEL = panel
    _RUNTIME_BOOTSTRAP_PENDING = False


def _bootstrap_runtime() -> None:
    global _RUNTIME_BOOTSTRAP_PENDING
    _RUNTIME_BOOTSTRAP_PENDING = False
    try:
        initialize_runtime()
    except Exception as exc:
        try:
            App.Console.PrintError("GitSync: unable to initialize the panel: {}\n".format(exc))
        except Exception:
            print("GitSync: unable to initialize the panel: {}".format(exc))


def shutdown_runtime() -> None:
    """Detach FreeCAD observers and clear cached runtime references."""
    global _ACTIVE_PANEL
    global _RUNTIME_CONTEXT
    global _RUNTIME_DOCK
    global _RUNTIME_CONTROLLER
    if _RUNTIME_CONTROLLER is not None:
        try:
            _RUNTIME_CONTROLLER.shutdown()
        except Exception:
            pass
    dock = _RUNTIME_DOCK
    if dock is not None:
        try:
            parent = dock.parentWidget()
            if parent is not None:
                parent.removeDockWidget(dock)
            dock.deleteLater()
        except Exception:
            pass
    _RUNTIME_CONTROLLER = None
    _RUNTIME_DOCK = None
    _RUNTIME_CONTEXT = None
    _ACTIVE_PANEL = None


class _PanelCommand:
    def __init__(self, callback_name: str, label: str = "GitSync") -> None:
        self.callback_name = callback_name
        self.label = label

    def GetResources(self):  # noqa: N802 - FreeCAD API name
        return {"MenuText": self.label, "ToolTip": self.label}

    def Activated(self) -> None:
        panel = _panel()
        if panel is not None:
            getattr(panel, self.callback_name)()

    def IsActive(self) -> bool:
        panel = _panel()
        return panel is not None and not panel.context.runner.busy

    def GetClassName(self) -> str:  # noqa: N802 - FreeCAD API name
        return "Gui::PythonCommand"


class GitSyncWorkbench(_WorkbenchBase):
    """The GitSync workbench and its persistent dock panel."""

    MenuText = "GitSync"
    ToolTip = "Synchronize FreeCAD models with a remote Git repository"

    def __init__(self) -> None:
        super().__init__()
        self._context = None
        self._panel = None
        self._dock = None
        self._controller = None
        self._initialized = False

    def Initialize(self) -> None:  # noqa: N802 - FreeCAD API name
        if self._initialized:
            return
        initialize_runtime()
        self._context = _RUNTIME_CONTEXT
        self._panel = _RUNTIME_DOCK.widget() if _RUNTIME_DOCK is not None else None
        self._dock = _RUNTIME_DOCK
        self._controller = _RUNTIME_CONTROLLER

        # Ordered like the panel's own groups: repository, then synchronization,
        # then creating a model.  Connect / Clone stays in the menu only - the
        # panel has no such button because Settings carries the same fields.
        self.appendToolbar(
            "GitSync",
            [
                "GitSync_Settings",
                "GitSync_Refresh",
                "GitSync_Pull",
                "GitSync_Push",
                "GitSync_NewModel",
            ],
        )
        self.appendMenu(
            "GitSync",
            [
                "GitSync_Connect",
                "GitSync_Settings",
                "GitSync_Refresh",
                "GitSync_Pull",
                "GitSync_Push",
                "GitSync_NewModel",
            ],
        )
        self.appendCommandbar("GitSync", ["GitSync_NewModel", "GitSync_Pull"])
        self._initialized = True

    def Activated(self) -> None:  # noqa: N802 - FreeCAD API name
        if self._dock is None:
            initialize_runtime()
            self._context = _RUNTIME_CONTEXT
            self._panel = _RUNTIME_DOCK.widget() if _RUNTIME_DOCK is not None else None
            self._dock = _RUNTIME_DOCK
            self._controller = _RUNTIME_CONTROLLER
        if self._dock is not None:
            self._dock.show()
            self._dock.raise_()

    def Deactivated(self) -> None:  # noqa: N802 - FreeCAD API name
        # Keep the panel available so its status remains visible when the user
        # switches to another workbench.
        return

    def GetClassName(self) -> str:  # noqa: N802 - FreeCAD API name
        return "Gui::PythonWorkbench"


def workbench_icon_path() -> str:
    """Return the workbench tab icon, or an empty string when it is missing.

    FreeCAD resolves the tab icon of its own workbenches from the install tree,
    but an addon has to name the file itself, so the path is handed to
    ``Workbench.Icon`` before the workbench is registered.
    """
    for relative in (
        os.path.join("icons", "GitSyncWorkbench.svg"),
        os.path.join("icons", "GitSyncWorkbench.png"),
        os.path.join("Resources", "icons", "GitSyncWorkbench.svg"),
    ):
        candidate = os.path.join(_THIS_DIR, relative)
        if os.path.isfile(candidate):
            return candidate
    return ""


# Commands are kept in this module so FreeCAD can register them while loading
# InitGui.py.  They are intentionally tiny wrappers around the panel methods.
class ConnectCommand(_PanelCommand):
    def __init__(self) -> None:
        super().__init__("open_setup", "Connect / Clone")


class RefreshCommand(_PanelCommand):
    def __init__(self) -> None:
        # refresh_and_check, not refresh: Refresh behaves like a fresh start,
        # so it fetches the remote and raises the startup dialog when needed.
        super().__init__("refresh_and_check", "Refresh GitSync")


class PushCommand(_PanelCommand):
    def __init__(self) -> None:
        super().__init__("push", "Push commits")


class PullCommand(_PanelCommand):
    def __init__(self) -> None:
        super().__init__("pull", "Pull changes")


class NewModelCommand(_PanelCommand):
    def __init__(self) -> None:
        super().__init__("new_model", "New model in repository")


class SettingsCommand(_PanelCommand):
    def __init__(self) -> None:
        super().__init__("open_settings", "GitSync settings")


__all__ = [
    "GitSyncWorkbench",
    "initialize_runtime",
    "shutdown_runtime",
    "workbench_icon_path",
    "ConnectCommand",
    "NewModelCommand",
    "RefreshCommand",
    "PushCommand",
    "PullCommand",
    "SettingsCommand",
]
