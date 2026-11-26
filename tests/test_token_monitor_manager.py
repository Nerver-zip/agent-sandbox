import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/token-monitor"))
import manager  # noqa: E402


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.config = self.project / ".token-monitor.json"
        self.state = self.project / ".token-monitor"
        self.secret = self.project / ".token-monitor.secret"
        self.units = self.project / "units"
        self.units.mkdir()
        paths = patch.multiple(manager, PROJECT=self.project, CONFIG=self.config,
                               STATE=self.state, SYSTEMD=self.units)
        paths.start()
        self.addCleanup(paths.stop)

    def write(self, **options):
        self.config.write_text(json.dumps({
            "enabled": True, "hub_url": "http://127.0.0.1:17321",
            "secret_file": ".token-monitor.secret", **options,
        }))
        self.config.chmod(0o600)
        self.secret.write_text("fixture-secret\n")
        self.secret.chmod(0o600)

    def test_default_and_explicit_opt_out_need_no_hub(self):
        self.assertIsNone(manager.configuration())
        self.config.write_text('{"enabled": false}')
        self.config.chmod(0o600)
        self.assertIsNone(manager.configuration())
        self.assertFalse(self.state.exists())
        self.assertEqual(list(self.units.iterdir()), [])

    def test_provider_default_and_explicit_upgrade(self):
        self.write()
        self.assertEqual(manager.configuration()["providers"], ["claude", "codex", "opencode", "antigravity"])
        self.write(providers=["claude", "codex", "opencode"])
        self.assertEqual(manager.configuration()["providers"], ["claude", "codex", "opencode"])
        self.write(providers=["claude", "codex", "opencode", "antigravity"])
        self.assertEqual(manager.configuration()["providers"][-1], "antigravity")

    def test_projects_are_enabled_by_default_and_can_be_disabled(self):
        self.write()
        self.assertIs(manager.configuration()["projects_enabled"], True)
        self.write(projects_enabled=False)
        self.assertIs(manager.configuration()["projects_enabled"], False)
        self.write(projects_enabled="false")
        with self.assertRaisesRegex(manager.ConfigurationError, "projects_enabled must be a boolean"):
            manager.configuration()

    def test_secret_path_and_url_validation(self):
        self.write(hub_url="https://fixture-user:bad@example.net")
        with self.assertRaises(manager.ConfigurationError):
            manager.configuration()
        self.write(secret_file="Dockerfile")
        with self.assertRaises(manager.ConfigurationError):
            manager.configuration()
        self.write()
        self.secret.chmod(0o644)
        with self.assertRaises(manager.ConfigurationError):
            manager.configuration()
        self.secret.unlink()
        self.secret.symlink_to(self.config)
        with self.assertRaises(OSError):
            manager.configuration()

    def test_device_persists_and_migration_uses_explicit_id(self):
        self.write(device_id="migration-fixture")
        self.state.mkdir()
        # Monitor hashes only its image build inputs.
        shutil.copytree(ROOT / "scripts/token-monitor", self.project / "scripts/token-monitor")
        with self.assertRaisesRegex(manager.ConfigurationError, "session archive is missing"):
            manager.Monitor(manager.configuration())
        self.assertFalse((self.state / "device.json").exists())
        archive = self.state / "collector/archive/session-usage-archive.json"
        archive.parent.mkdir(parents=True)
        archive.write_text('{"sessions":{}}')
        self.assertEqual(manager.Monitor(manager.configuration()).device, "migration-fixture")
        self.write(device_prefix="new-prefix")
        self.assertEqual(manager.Monitor(manager.configuration()).device, "migration-fixture")
        manager.durable_json(self.state / "status.json", {"last_success": 1})
        archive.unlink()
        with self.assertRaisesRegex(manager.ConfigurationError, "session archive is missing"):
            manager.Monitor(manager.configuration())
        self.write(device_id="other-device")
        with self.assertRaises(manager.ConfigurationError):
            manager.Monitor(manager.configuration())

    def test_unit_setup_is_idempotent_and_uninstall_cleans_only_owned_units(self):
        with patch.object(manager, "systemctl") as systemctl:
            manager.manage_units()
            files = sorted(self.units.iterdir())
            inodes = [path.stat().st_ino for path in files]
            manager.manage_units()
            self.assertEqual([path.stat().st_ino for path in sorted(self.units.iterdir())], inodes)
            if shutil.which("systemd-analyze"):
                subprocess.run(["systemd-analyze", "verify", *map(str, files)], check=True,
                               capture_output=True, timeout=10)
            manager.manage_units(remove=True)
            self.assertEqual(list(self.units.iterdir()), [])
            calls = systemctl.call_count
            manager.manage_units(remove=True)
            self.assertEqual(systemctl.call_count, calls)
            files[0].write_text("# external unit\n")
            with self.assertRaises(manager.ConfigurationError):
                manager.manage_units()
            self.assertEqual(files[0].read_text(), "# external unit\n")


if __name__ == "__main__":
    unittest.main()
