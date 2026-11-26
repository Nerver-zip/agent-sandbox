import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
        self.write(collector_version="v0.98.0", providers=["antigravity"])
        with self.assertRaisesRegex(manager.ConfigurationError, "requires the pinned collector version"):
            manager.configuration()
        self.write(collector_version="v0.98.0", providers=["claude"])
        self.assertEqual(manager.configuration()["providers"], ["claude"])

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

    def test_sanitizer_cleanup_distinguishes_absent_from_live_worker(self):
        self.write()
        self.state.mkdir()
        shutil.copytree(ROOT / "scripts/token-monitor", self.project / "scripts/token-monitor")
        monitor = manager.Monitor(manager.configuration())
        with patch.object(manager, "docker", side_effect=[manager.JobError("missing"), ""]):
            monitor.remove_sanitizer("fixture-sanitizer")
        with patch.object(manager, "docker", side_effect=[manager.JobError("failed"), "fixture-sanitizer\n"]):
            with self.assertRaises(manager.JobError):
                monitor.remove_sanitizer("fixture-sanitizer")
        with patch.object(manager, "docker", side_effect=[manager.JobError("failed"), manager.JobError("daemon")]):
            with self.assertRaises(manager.JobError):
                monitor.remove_sanitizer("fixture-sanitizer")

    def test_sanitizer_rejects_hosts_without_swap_limit(self):
        self.write()
        self.state.mkdir()
        shutil.copytree(ROOT / "scripts/token-monitor", self.project / "scripts/token-monitor")
        monitor = manager.Monitor(manager.configuration())
        with patch.object(manager, "docker", return_value="false"), \
             self.assertRaisesRegex(manager.SnapshotError, "swap-limit support"):
            monitor.sanitize_antigravity([], self.project / "output")
        self.assertFalse((self.project / "output").exists())

    def test_sanitizer_timeout_removes_worker_and_kills_client(self):
        self.write()
        self.state.mkdir()
        shutil.copytree(ROOT / "scripts/token-monitor", self.project / "scripts/token-monitor")
        monitor = manager.Monitor(manager.configuration())
        source = self.project / "fixture.db"
        source.write_bytes(b"fixture")
        fd = os.open(source, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        process = Mock()
        process.stdin = io.BytesIO()
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 1), 0]
        with patch.object(manager, "docker", return_value="true"), patch.object(manager.subprocess, "Popen", return_value=process), \
             patch.object(manager.subprocess, "run") as run:
            run.return_value.returncode = 0
            with self.assertRaisesRegex(manager.SnapshotError, "timed out"):
                monitor.sanitize_antigravity([("session.db", fd, [0, 0, 7, 0, 0]),
                                            ("session.db-wal", None, None),
                                            ("session.db-journal", None, None)], self.project / "output")
        process.kill.assert_called_once()
        self.assertIn("rm", run.call_args.args[0])

    def test_legacy_raw_antigravity_snapshot_is_not_delivered(self):
        self.write()
        self.state.mkdir()
        shutil.copytree(ROOT / "scripts/token-monitor", self.project / "scripts/token-monitor")
        monitor = manager.Monitor(manager.configuration())
        generation = self.state / "old-generation"
        generation.mkdir()
        (generation / "index.json").write_text(json.dumps({
            ".gemini/antigravity-cli/conversations/session.db": [[1, 2, 3, 4, 5], None, None]
        }))
        with self.assertRaisesRegex(manager.JobError, "predates sanitization"):
            monitor.deliver(generation)
        self.assertTrue(generation.is_dir())

    def test_final_capture_with_isolated_conversation_failure_blocks_removal(self):
        self.write()
        self.state.mkdir()
        shutil.copytree(ROOT / "scripts/token-monitor", self.project / "scripts/token-monitor")
        monitor = manager.Monitor(manager.configuration())
        monitor.snapshots.mkdir()
        generation = monitor.snapshots / ("0" * 20 + "-" + "a" * 32)
        generation.mkdir()
        (generation / "issues.json").write_text(json.dumps(["a" * 64]))
        fd = os.open(self.project, os.O_RDONLY)
        with patch.object(manager, "open_workspace", return_value=fd), \
             patch.object(manager, "docker"), patch.object(monitor, "publish"), \
             self.assertRaisesRegex(manager.JobError, "final capture is incomplete"):
            monitor.capture_workspace("fixture", 123, "start", stop=True)
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["antigravity_incomplete"], ["a" * 64])

    def test_partial_delivery_reports_incomplete_without_failing_other_providers(self):
        self.write()
        self.state.mkdir()
        shutil.copytree(ROOT / "scripts/token-monitor", self.project / "scripts/token-monitor")
        monitor = manager.Monitor(manager.configuration())
        monitor.snapshots.mkdir()
        pending = monitor.snapshots / ("0" * 20 + "-" + "b" * 32)
        pending.mkdir()
        stderr = io.StringIO()
        with patch.object(manager, "workspace", return_value=("fixture", 123, "start")), \
             patch.object(monitor, "deliver", return_value=["a" * 64]), \
             patch.object(sys, "stderr", stderr):
            monitor.sync()
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["antigravity_incomplete"], ["a" * 64])
        self.assertIn("incomplete", stderr.getvalue())

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
