"""Tests for GitSync's standalone configuration store."""

from __future__ import annotations

import unittest

from gitsync.configuration import SettingsStore, _MemoryParameters


class ConfigurationTests(unittest.TestCase):
    def test_settings_round_trip(self) -> None:
        parameters = _MemoryParameters()
        values = {
            "server_url": "http://example.test/repo.git",
            "username": "user",
            "token": "token-value",
            "workspace": "/tmp/workspace",
            "preview_home": False,
            "auto_commit_save": True,
            "auto_push_save": True,
            "auto_commit_message": "custom",
            "custom_commit_message": "Update {filename}",
            "model_sort": "modified",
            "push_on_exit": True,
            "allow_insecure_http": True,
            "trusted_repository": "",
        }
        SettingsStore(parameters).save(values)
        loaded = SettingsStore(parameters).load()
        self.assertEqual(loaded, values)

    def test_repository_trust_is_stored_and_cleared_with_the_target(self) -> None:
        from gitsync.configuration import canonical_remote_target

        parameters = _MemoryParameters()
        store = SettingsStore(parameters)
        target = canonical_remote_target("https://example.test/repo.git", "user")
        store.save(
            {
                "server_url": "https://example.test/repo.git",
                "username": "user",
                "workspace": "/tmp/workspace",
                "trusted_repository": target,
            }
        )
        self.assertEqual(store.load()["trusted_repository"], target)
        # A differently spelled but equivalent target keeps the trust.
        store.save(
            {
                "server_url": "https://Example.test/repo.git/",
                "username": "user",
                "workspace": "/tmp/workspace",
                "trusted_repository": target,
            }
        )
        self.assertEqual(store.load()["trusted_repository"], target)
        # Connecting somewhere else revokes it.
        store.save(
            {
                "server_url": "https://other.test/repo.git",
                "username": "user",
                "workspace": "/tmp/other",
                "trusted_repository": target,
            }
        )
        self.assertEqual(store.load()["trusted_repository"], "")

    def test_canonical_target_ignores_trailing_slash_and_url_case(self) -> None:
        from gitsync.configuration import canonical_remote_target

        self.assertEqual(
            canonical_remote_target("HTTPS://Example.test/repo.git/", "User"),
            canonical_remote_target("https://example.test/repo.git", "User"),
        )

    def test_changing_connection_clears_old_fallback_token(self) -> None:
        parameters = _MemoryParameters()
        store = SettingsStore(parameters)
        store.save(
            {
                "server_url": "https://one.test/repo.git",
                "username": "user",
                "token": "old-token",
                "workspace": "/tmp/one",
                "preview_home": True,
                "auto_push_save": False,
                "push_on_exit": False,
            }
        )
        store.save(
            {
                "server_url": "https://two.test/repo.git",
                "username": "user",
                "token": "new-token",
                "workspace": "/tmp/two",
                "preview_home": True,
                "auto_push_save": False,
                "push_on_exit": False,
            }
        )
        self.assertEqual(store.load()["token"], "new-token")
        self.assertNotIn("old-token", str(parameters._values))


if __name__ == "__main__":
    unittest.main()
