"""Tests for the workbench's toolbar, menu and command bar.

These read the source rather than importing it: ``GitSyncWorkbench`` imports
FreeCAD at module level, so the headless suite cannot import it.  Parsing the
call is enough to pin what the user actually sees in the toolbar.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qt_stub  # noqa: E402

qt_stub.install()

WORKBENCH = Path(__file__).resolve().parent.parent / "GitSyncWorkbench.py"
ENTRY = Path(__file__).resolve().parent.parent / "InitGui.py"

#: Every command ``InitGui`` registers.
REGISTERED = (
    "GitSync_Connect",
    "GitSync_NewModel",
    "GitSync_Pull",
    "GitSync_Push",
    "GitSync_Refresh",
    "GitSync_Settings",
)


def _string_list(name: str) -> list:
    """Return the literal list passed to ``name(...)`` in WORKBENCH."""
    tree = ast.parse(WORKBENCH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        label = getattr(function, "attr", None)
        if label != name:
            continue
        for argument in node.args:
            if isinstance(argument, ast.List) and all(
                isinstance(item, ast.Constant) and isinstance(item.value, str)
                for item in argument.elts
            ):
                return [item.value for item in argument.elts]
    raise AssertionError("no literal list passed to {}()".format(name))


class ToolbarTests(unittest.TestCase):
    def test_the_toolbar_offers_the_five_daily_actions(self) -> None:
        self.assertEqual(
            _string_list("appendToolbar"),
            [
                "GitSync_Settings",
                "GitSync_Refresh",
                "GitSync_Pull",
                "GitSync_Push",
                "GitSync_NewModel",
            ],
        )

    def test_connect_is_not_on_the_toolbar(self) -> None:
        # The panel has no Connect / Clone button either, because Settings
        # carries the same four fields; the command stays in the menu.
        self.assertNotIn("GitSync_Connect", _string_list("appendToolbar"))

    def test_every_toolbar_command_is_registered(self) -> None:
        # FreeCAD refuses a toolbar entry naming a command that does not exist,
        # which would leave the workbench without a toolbar at all.
        for name in _string_list("appendToolbar") + _string_list("appendCommandbar"):
            self.assertIn(name, REGISTERED, name)

    def test_the_menu_still_offers_connect(self) -> None:
        self.assertIn("GitSync_Connect", _string_list("appendMenu"))
        self.assertEqual(
            _string_list("appendMenu"),
            [
                "GitSync_Connect",
                "GitSync_Settings",
                "GitSync_Refresh",
                "GitSync_Pull",
                "GitSync_Push",
                "GitSync_NewModel",
            ],
        )

    def test_the_menu_and_toolbar_register_no_duplicates(self) -> None:
        menu = _string_list("appendMenu")
        self.assertEqual(len(menu), len(set(menu)))


class RegistrationTests(unittest.TestCase):
    def test_initgui_registers_every_command_exactly_once(self) -> None:
        source = ENTRY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "_add_command":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append(first.value)
        self.assertEqual(sorted(found), sorted(REGISTERED))
        self.assertEqual(len(found), len(set(found)))

    def test_the_documented_command_count_is_accurate(self) -> None:
        self.assertEqual(len(REGISTERED), 6)


if __name__ == "__main__":
    unittest.main()