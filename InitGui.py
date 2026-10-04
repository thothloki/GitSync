"""GitSync FreeCAD GUI entry point."""

import os
import sys

import FreeCAD as App
import FreeCADGui as Gui

# InitGui.py is executed by FreeCAD without a reliable __file__.  Find the
# directory containing this uniquely named entry module and put it first so
# generic helper modules cannot be shadowed by another Mod directory.
for _entry in list(sys.path):
    if os.path.isfile(os.path.join(_entry, "GitSyncWorkbench.py")):
        if _entry not in sys.path[:1]:
            sys.path.insert(0, _entry)
        break

from GitSyncWorkbench import (
    ConnectCommand,
    GitSyncWorkbench,
    NewModelCommand,
    PullCommand,
    PushCommand,
    RefreshCommand,
    SettingsCommand,
    _bootstrap_runtime,
    workbench_icon_path,
)

# Register commands once when FreeCAD loads this Mod directory.  The guards
# make a manual InitGui reload harmless during development.
def _add_command(name, command) -> None:
    try:
        Gui.addCommand(name, command)
    except Exception as exc:
        App.Console.PrintWarning("GitSync: command {} was already registered ({})".format(name, exc))


_add_command("GitSync_Connect", ConnectCommand())
_add_command("GitSync_Refresh", RefreshCommand())
_add_command("GitSync_Push", PushCommand())
_add_command("GitSync_Pull", PullCommand())
_add_command("GitSync_NewModel", NewModelCommand())
_add_command("GitSync_Settings", SettingsCommand())

# Install the panel after the GUI event loop is available.  This makes the
# startup repository comparison run even if the user has not selected the
# GitSync workbench yet.
try:
    from gitsync.qt_compat import QtCore

    QtCore.QTimer.singleShot(0, _bootstrap_runtime)
except Exception:
    # The workbench's Initialize method will retry when it is selected.
    pass

try:
    # The tab icon has to be named explicitly: FreeCAD only resolves the icons
    # of its own workbenches from the install tree.
    workbench = GitSyncWorkbench()
    icon = workbench_icon_path()
    if icon:
        workbench.Icon = icon
    Gui.addWorkbench(workbench)
except Exception as exc:
    App.Console.PrintWarning("GitSync: workbench was already registered ({})".format(exc))
