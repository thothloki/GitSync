"""Tests for the settings dialog's value round-trip.

A missing attribute in ``values()`` used to raise, which made the whole
settings save fail while the dialog looked like it had worked.  These tests
call ``values()`` directly so that class of bug fails here instead.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qt_stub  # noqa: E402

qt_stub.install()

from gitsync.configuration import SettingsStore, _MemoryParameters  # noqa: E402
from gitsync.dialogs import SettingsDialog  # noqa: E402
from gitsync.git_service import canonical_remote_target  # noqa: E402

URL = "https://example.test/repo.git"
USER = "andy"

BASE = {
    "server_url": URL,
    "username": USER,
    "token": "",
    "workspace": "/tmp/workspace",
    "preview_home": True,
    "auto_commit_save": True,
    "auto_push_save": False,
    "auto_commit_message": "timestamp",
    "custom_commit_message": "",
    "model_sort": "name",
    "push_on_exit": False,
    "allow_insecure_http": False,
    "trusted_repository": "",
}


class _Box:
    """Stand-in for a checkbox with a settable state."""

    def __init__(self, checked: bool) -> None:
        self.checked = checked

    def isChecked(self) -> bool:  # noqa: N802 - Qt API name
        return self.checked


class _Form:
    """Stand-in for ConnectionForm returning known field values."""

    def __init__(self, values) -> None:
        self._values = dict(values)

    def values(self):
        return dict(self._values)


def _dialog(values=None, checks=None, form=None):
    """Build a SettingsDialog with its widgets replaced by simple fakes."""
    settings = dict(BASE if values is None else values)
    store = SettingsStore(_MemoryParameters())
    dialog = SettingsDialog(settings, store, None)
    dialog.form = _Form(form if form is not None else {
        "server_url": settings["server_url"],
        "username": settings["username"],
        "token": settings.get("token", ""),
        "workspace": settings["workspace"],
    })
    states = {
        "preview_home": settings["preview_home"],
        "auto_commit_save": settings["auto_commit_save"],
        "auto_push_save": settings["auto_push_save"],
        "custom_commit_message": settings["auto_commit_message"] == "custom",
        "push_on_exit": settings["push_on_exit"],
        "allow_insecure_http": settings["allow_insecure_http"],
        "trust_repository": bool(settings["trusted_repository"]),
    }
    states.update(checks or {})
    for name, checked in states.items():
        setattr(dialog, name, _Box(checked))
    return dialog, store


class SettingsDialogTests(unittest.TestCase):
    def test_values_returns_every_setting_the_store_knows(self) -> None:
        dialog, store = _dialog()
        values = dialog.values()
        self.assertEqual(sorted(values), sorted(store.load()))
        # Nothing may be missing or invented.
        for key in ("auto_commit_message", "custom_commit_message", "model_sort"):
            self.assertIn(key, values)

    def test_unchecking_the_message_prompt_saves_the_timestamp_mode(self) -> None:
        dialog, store = _dialog(
            values=dict(BASE, auto_commit_message="custom"),
            checks={"custom_commit_message": False},
        )
        self.assertEqual(dialog.values()["auto_commit_message"], "timestamp")
        saved = store.save(dialog.values())
        self.assertEqual(saved["auto_commit_message"], "timestamp")
        self.assertEqual(store.load()["auto_commit_message"], "timestamp")

    def test_checking_the_message_prompt_saves_the_custom_mode(self) -> None:
        dialog, store = _dialog(checks={"custom_commit_message": True})
        self.assertEqual(dialog.values()["auto_commit_message"], "custom")
        self.assertEqual(store.save(dialog.values())["auto_commit_message"], "custom")

    def test_message_mode_survives_a_save_of_the_other_settings(self) -> None:
        dialog, store = _dialog(
            values=dict(BASE, auto_commit_message="custom", custom_commit_message="Workshop"),
            checks={"custom_commit_message": True, "push_on_exit": True},
        )
        saved = store.save(dialog.values())
        self.assertEqual(saved["auto_commit_message"], "custom")
        # The remembered message is passed through untouched.
        self.assertEqual(saved["custom_commit_message"], "Workshop")
        self.assertTrue(saved["push_on_exit"])

    def test_model_sort_is_passed_through(self) -> None:
        dialog, store = _dialog(values=dict(BASE, model_sort="modified"))
        values = dialog.values()
        self.assertEqual(values["model_sort"], "modified")
        self.assertEqual(store.save(values)["model_sort"], "modified")

    def test_unknown_model_sort_falls_back_to_name(self) -> None:
        dialog, _store = _dialog(values=dict(BASE, model_sort="nonsense"))
        self.assertEqual(dialog.values()["model_sort"], "name")

    def test_trust_is_dropped_when_the_connection_fields_are_edited(self) -> None:
        target = canonical_remote_target(URL, USER)
        trusted = dict(BASE, trusted_repository=target)
        dialog, _store = _dialog(
            values=trusted,
            checks={"trust_repository": True},
            form={"server_url": "https://other.test/repo.git", "username": USER,
                  "token": "", "workspace": "/tmp/workspace"},
        )
        self.assertEqual(dialog.values()["trusted_repository"], "")

    def test_trust_is_kept_for_an_unchanged_target(self) -> None:
        target = canonical_remote_target(URL, USER)
        dialog, _store = _dialog(values=dict(BASE, trusted_repository=target),
                                checks={"trust_repository": True})
        self.assertEqual(dialog.values()["trusted_repository"], target)

    def test_values_only_reads_attributes_its_class_assigns(self) -> None:
        """Guard every dialog against reading an attribute it never sets.

        The Qt stub answers any attribute request with a placeholder, so a
        missing ``self._foo`` is invisible at runtime in the tests; this static
        check catches it.  It matters because such a read makes ``values()``
        raise, which silently discards every setting the user just saved.
        """
        import ast
        import inspect
        import textwrap

        from gitsync import dialogs

        def self_attributes(method):
            """(name, is_assignment) for every ``self.x`` in a method body."""
            try:
                source = textwrap.dedent(inspect.getsource(method))
            except (OSError, TypeError):
                return set()
            tree = ast.parse(source)
            found = set()
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                ):
                    found.add((node.attr, isinstance(node.ctx, ast.Store)))
            return found

        checked = []
        for class_name in dir(dialogs):
            klass = getattr(dialogs, class_name)
            if (
                not inspect.isclass(klass)
                or getattr(klass, "__module__", "") != dialogs.__name__
                or not callable(getattr(klass, "values", None))
            ):
                continue
            checked.append(class_name)
            assigned = set()
            for member in vars(klass).values():
                if not callable(member):
                    continue
                assigned |= {n for n, stored in self_attributes(member) if stored}
            missing = sorted(
                name
                for name, stored in self_attributes(klass.values)
                if not stored and name not in assigned
            )
            self.assertEqual(missing, [], f"{class_name}.values() reads unset attributes")
        self.assertIn("SettingsDialog", checked)
        self.assertIn("SetupDialog", checked)
        self.assertIn("ConnectionForm", checked)


if __name__ == "__main__":
    unittest.main()