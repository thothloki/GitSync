"""Tests for revealing a model in the operating system's file browser."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from gitsync import os_integration
from gitsync.os_integration import (
    reveal_command,
    reveal_in_file_manager,
    select_command,
)


class SelectCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = Path("/home/user/models/drawers/Divider.FCStd")
        self.real_available = os_integration._dbus_available
        os_integration._dbus_available = lambda: True
        self.addCleanup(
            setattr, os_integration, "_dbus_available", self.real_available
        )

    def test_macos_reveals_and_selects(self) -> None:
        self.assertEqual(
            select_command(self.model, "darwin"), ["open", "-R", str(self.model)]
        )

    def test_windows_glues_the_select_switch_to_the_path(self) -> None:
        # explorer wants "/select,<path>" as one argument: no space, and no
        # quotes, because the whole thing is one argv entry.
        command = select_command(self.model, "win32")
        self.assertEqual(command, ["explorer", "/select,{}".format(self.model)])
        self.assertNotIn(" ", command[1])
        self.assertFalse(command[1].startswith('"'))

    def test_linux_uses_the_standard_file_manager_dbus_call(self) -> None:
        command = select_command(self.model, "linux")
        self.assertEqual(command[:2], ["dbus-send", "--session"])
        self.assertIn("--dest=org.freedesktop.FileManager1", command)
        self.assertIn(
            "org.freedesktop.FileManager1.ShowItems", command
        )
        # The model is sent as a file:// URI in an array of strings, with an
        # empty typed startup id after it.  dbus-send rejects a bare `""`.
        self.assertIn(
            "array:string:file:///home/user/models/drawers/Divider.FCStd", command
        )
        self.assertEqual(command[-1], "string:")
        # Waiting for the reply is what makes a fallback possible.
        self.assertTrue(any(part.startswith("--print-reply") for part in command))

    def test_linux_offers_nothing_without_a_session_bus(self) -> None:
        os_integration._dbus_available = lambda: False
        self.assertIsNone(select_command(self.model, "linux"))


class RevealCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = Path("/home/user/models/drawers/Divider.FCStd")

    def test_linux_opens_the_containing_folder(self) -> None:
        # The fallback: opening the file itself would launch whatever
        # application handles .FCStd instead of showing a browser.
        self.assertEqual(
            reveal_command(self.model, "linux"),
            ["xdg-open", "/home/user/models/drawers"],
        )

    def test_the_default_platform_is_this_interpreter(self) -> None:
        self.assertEqual(
            reveal_command(self.model), reveal_command(self.model, sys.platform)
        )


class RevealInFileManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent
        self.model = self.root / "test_os_integration.py"
        self.real_available = os_integration._dbus_available
        os_integration._dbus_available = lambda: False
        self.addCleanup(
            setattr, os_integration, "_dbus_available", self.real_available
        )

    def test_it_falls_back_to_the_folder_when_nothing_can_select(self) -> None:
        seen = []
        command = reveal_in_file_manager(self.model, "linux", runner=seen.append)
        self.assertEqual(seen, [["xdg-open", str(self.root)]])
        self.assertEqual(command, "xdg-open {}".format(self.root))

    def test_it_selects_when_the_platform_can(self) -> None:
        os_integration._dbus_available = lambda: True
        seen = []
        command = reveal_in_file_manager(self.model, "linux", runner=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][:2], ["dbus-send", "--session"])
        self.assertEqual(command, " ".join(seen[0]))

    def test_macos_only_ever_runs_one_command(self) -> None:
        seen = []
        reveal_in_file_manager(self.model, "darwin", runner=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], ["open", "-R", str(self.model)])

    def test_a_missing_model_is_refused_before_anything_runs(self) -> None:
        seen = []
        with self.assertRaises(FileNotFoundError):
            reveal_in_file_manager(
                self.root / "does-not-exist.FCStd", "linux", runner=seen.append
            )
        self.assertEqual(seen, [])


class DbusFallbackTests(unittest.TestCase):
    """The live path: a failing FileManager1 must fall back, not raise."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parent
        self.model = self.root / "test_os_integration.py"
        self.real_available = os_integration._dbus_available
        os_integration._dbus_available = lambda: True
        self.addCleanup(
            setattr, os_integration, "_dbus_available", self.real_available
        )
        self.launched = []
        real_popen = os_integration.subprocess.Popen
        os_integration.subprocess.Popen = lambda cmd, **kw: self.launched.append(cmd)
        self.addCleanup(
            setattr, os_integration.subprocess, "Popen", real_popen
        )

    def _stub_run(self, returncode):
        class _Completed:
            pass

        completed = _Completed()
        completed.returncode = returncode
        os_integration.subprocess.run = lambda *a, **kw: completed

    def test_a_failing_dbus_call_falls_back_to_xdg_open(self) -> None:
        real_run = os_integration.subprocess.run
        self.addCleanup(setattr, os_integration.subprocess, "run", real_run)
        self._stub_run(returncode=1)
        command = reveal_in_file_manager(self.model, "linux")
        self.assertTrue(command.startswith("xdg-open"))
        self.assertEqual(self.launched, [["xdg-open", str(self.root)]])

    def test_a_successful_dbus_call_does_not_launch_xdg_open(self) -> None:
        real_run = os_integration.subprocess.run
        self.addCleanup(setattr, os_integration.subprocess, "run", real_run)
        self._stub_run(returncode=0)
        command = reveal_in_file_manager(self.model, "linux")
        self.assertTrue(command.startswith("dbus-send"))
        self.assertEqual(self.launched, [])


if __name__ == "__main__":
    unittest.main()