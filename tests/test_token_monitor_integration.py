"""Opt-in integration with the official Node Hub and built collector image."""

from contextlib import closing
import datetime
import json
import os
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/token-monitor"))
import manager  # noqa: E402
import antigravity_projection  # noqa: E402
from relay import Hub, HubError  # noqa: E402
from snapshot import DIR_FLAGS  # noqa: E402

UPSTREAM = os.environ.get("TOKEN_MONITOR_TEST_UPSTREAM")
IMAGE = os.environ.get("TOKEN_MONITOR_TEST_IMAGE")


@unittest.skipUnless(UPSTREAM and IMAGE, "requires an upstream checkout and a built collector image")
class OfficialCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.source = self.path / "source"
        self.home = self.source / "home/agent"
        self.home.mkdir(parents=True)
        self.secret = "isolated-fixture-secret"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.hub = Hub(f"http://127.0.0.1:{self.port}", self.secret)
        self.hub_process = None
        self.addCleanup(self.stop_hub)
        self.start_hub()
        self.state = self.path / "monitor"
        self.state.mkdir()
        self.saved_state, manager.STATE = manager.STATE, self.state
        self.addCleanup(setattr, manager, "STATE", self.saved_state)
        self.monitor = manager.Monitor({
            "hub": self.hub, "device": None, "prefix": "acceptance", "interval": 60,
            "version": manager.DEFAULT_VERSION, "providers": ["claude", "codex", "opencode"], "timezone": "UTC", "projects_enabled": True,
        })
        self.device = self.monitor.device
        self.monitor.image = IMAGE
        self.monitor.container_name = f"token-monitor-test-{os.getpid()}-{self.port}"
        self.monitor.snapshots.mkdir()
        self.monitor.collector_state.mkdir()
        saved_uid, manager.UID = manager.UID, os.getuid()
        self.addCleanup(setattr, manager, "UID", saved_uid)
        self.sources = self.write_usage()

    def start_hub(self):
        self.hub_process = subprocess.Popen(
            ["node", "src/hub/server.js"], cwd=UPSTREAM, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "TOKEN_MONITOR_SECRET": self.secret, "TOKEN_MONITOR_PORT": str(self.port),
                 "TOKEN_MONITOR_HOST": "127.0.0.1", "TOKEN_MONITOR_DATA_FILE": str(self.path / "hub.json")},
        )
        for _ in range(50):
            if self.hub_process.poll() is not None:
                break
            try:
                self.hub.probe()
                return
            except HubError:
                time.sleep(0.1)
        self.fail("The isolated official Hub did not start")

    def stop_hub(self):
        if self.hub_process and self.hub_process.poll() is None:
            self.hub_process.terminate()
            try:
                self.hub_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.hub_process.kill()
                self.hub_process.wait()

    def write_usage(self):
        stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)
        when = stamp.isoformat()
        ms = int(stamp.timestamp() * 1000)
        claude = self.home / ".claude/projects/fixture/claude-session.jsonl"
        codex = self.home / f".codex/sessions/{stamp:%Y/%m/%d}/rollout-{stamp:%Y-%m-%d}T10-00-00-codex-session.jsonl"
        codex.parent.mkdir(parents=True)
        claude.parent.mkdir(parents=True)
        claude.write_text(json.dumps({
            "type": "assistant", "sessionId": "claude-session", "timestamp": when, "requestId": "claude-request",
            "cwd": "/home/agent/workspace/Claude-Project",
            "message": {"id": "claude-message", "role": "assistant", "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 100, "output_tokens": 10}},
        }) + "\n")
        usage = {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 20,
                 "reasoning_output_tokens": 0, "total_tokens": 220}
        codex.write_text("".join(json.dumps({"timestamp": when, **record}) + "\n" for record in [
            {"type": "session_meta", "payload": {"id": "codex-session", "cwd": "/home/agent/workspace/Codex-Project",
                                                 "originator": "codex_cli_rs", "source": "cli"}},
            {"type": "turn_context", "payload": {"turn_id": "turn", "model": "gpt-5.4"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage,
                                                                           "last_token_usage": usage}}},
        ]))
        opencode = self.home / ".local/share/opencode"
        legacy = opencode / "storage/message/ses_legacy/msg_legacy.json"
        legacy.parent.mkdir(parents=True)
        message = {"id": "msg_legacy", "sessionID": "ses_legacy", "role": "assistant", "modelID": "gpt-4o",
                   "providerID": "openai", "path": {"root": "/home/agent/workspace/OpenCode-Legacy"}, "time": {"created": ms, "completed": ms + 1},
                   "tokens": {"input": 40, "output": 4, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
        legacy.write_text(json.dumps(message))
        db = opencode / "opencode.db"
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.executescript("""
            PRAGMA journal_mode=wal;
            CREATE TABLE session_v2(id TEXT PRIMARY KEY, directory TEXT, title TEXT);
            CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT, type TEXT, data TEXT);
            INSERT INTO session_v2 VALUES ('ses_current', '/home/agent/workspace/OpenCode-Current', 'private fixture title');
        """)
        message.pop("path")
        message.update(id="msg_current", sessionID="ses_current", tokens={
            "input": 80, "output": 8, "reasoning": 0, "cache": {"read": 0, "write": 0},
        })
        writer.execute("INSERT INTO session_message VALUES (?, ?, ?, ?)",
                       ("msg_current", "ses_current", "assistant", json.dumps(message)))
        writer.commit()
        return claude, codex, legacy, db

    def publish(self):
        fd = os.open(self.source, DIR_FLAGS)
        try:
            self.monitor.publish(fd, "fixture-container")
        finally:
            os.close(fd)

    def device_record(self):
        status, body = self.hub.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        self.assertNotIn(b"private fixture title", body)
        self.assertNotIn(b"/home/agent/workspace/", body)
        return next(record for record in json.loads(body)["devices"] if record["deviceId"] == self.device)

    def totals(self):
        period = self.device_record()["periods"]["allTime"]
        return period["clients"], period["clientCosts"]

    def projects(self):
        period = self.device_record()["periods"]["allTime"]
        return period["projects"]

    def test_projected_generation_matches_pinned_scanner(self):
        def number(field, value):
            return antigravity_projection.enc(field << 3) + antigravity_projection.enc(value)

        def blob(field, value):
            return (antigravity_projection.enc((field << 3) | 2) +
                    antigravity_projection.enc(len(value)) + value)

        source_dir = self.path / "projection-source/.gemini/antigravity-cli/conversations"
        target_dir = self.path / "projection-output/.gemini/antigravity-cli/conversations"
        source_dir.mkdir(parents=True)
        target_dir.mkdir(parents=True)
        source = source_dir / "session.db"
        target = target_dir / "session.db"
        fake_secret = b'{"access_token":"fixture-private-value"}'
        usage = number(2, 500) + number(5, 16000) + number(9, 40) + number(10, 300) + blob(11, b"response")
        row = blob(1, blob(2, blob(3, fake_secret)) + blob(4, usage) + blob(19, b"gemini-3-flash-a"))
        stamp = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).timestamp())
        meta = blob(1, blob(1, b"file:///home/agent/workspace/Fixture")) + blob(2, number(1, stamp))
        with closing(sqlite3.connect(source)) as db, db:
            db.executescript("CREATE TABLE gen_metadata(idx INTEGER,data BLOB); CREATE TABLE trajectory_metadata_blob(data BLOB); CREATE TABLE tokens(secret BLOB);")
            db.execute("INSERT INTO gen_metadata VALUES (0,?)", (row,))
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (meta,))
            db.execute("INSERT INTO tokens VALUES (?)", (fake_secret,))
        self.assertTrue(antigravity_projection.project_database(source, target))
        self.assertNotIn(fake_secret, target.read_bytes())

        def scan(home):
            output = subprocess.check_output([
                "docker", "run", "--rm", "--network=none",
                "--mount", f"type=bind,src={self.path},dst=/fixture,readonly",
                "--env", "TOKSCALE_CONFIG_DIR=/opt/tokscale-pricing",
                "--env", "TOKSCALE_PRICING_CACHE_ONLY=1",
                "--entrypoint", "/opt/token-monitor/node_modules/.bin/tokscale", IMAGE,
                "--offline", "--home", f"/fixture/{home}", "--client", "antigravity-cli",
                "--group-by", "client,workspace,session,model", "--json", "--no-spinner",
            ], timeout=60, stderr=subprocess.DEVNULL)
            result = json.loads(output)
            result.pop("processingTimeMs", None)
            return result
        original = scan("projection-source")
        projected = scan("projection-output")
        self.assertEqual(original, projected)
        self.assertEqual(original["totalInput"], 500)
        self.assertEqual(original["totalCacheRead"], 16000)
        self.assertEqual(original["totalOutput"], 300)
        self.assertEqual(original["entries"][0]["reasoning"], 40)
        self.assertGreater(original["totalCost"], 0)

    def test_malformed_antigravity_aborts_snapshot_and_removes_worker(self):
        directory = self.home / ".gemini/antigravity-cli/conversations"
        directory.mkdir(parents=True)
        secret = b"fixture-sensitive-input"
        with closing(sqlite3.connect(directory / "bad.db")) as db, db:
            db.execute("CREATE TABLE gen_metadata(idx INTEGER,data BLOB)")
            db.execute("INSERT INTO gen_metadata VALUES (0,?)", (b"\x0a\xff" + secret,))
        self.monitor.config["providers"].append("antigravity")
        with self.assertRaisesRegex(manager.SnapshotError, "sanitizer failed"):
            self.publish()
        self.assertEqual(self.monitor.pending(), [])
        self.assertIsNone(self.monitor._sanitizer_name)
        status = subprocess.run(["docker", "container", "inspect", self.monitor.container_name + "-sanitize"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        self.assertNotEqual(status.returncode, 0)
        for path in self.state.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(secret, path.read_bytes())

    def test_antigravity_upgrade_retention_and_restore(self):
        self.check_antigravity_retention(remove_one=False)

    def test_antigravity_duplicate_survives_partial_source_deletion(self):
        self.check_antigravity_retention(remove_one=True)

    def test_antigravity_monotonic_generation_update_and_restore(self):
        self.check_antigravity_retention(remove_one=False, rewrite=True)

    def check_antigravity_retention(self, remove_one, rewrite=False):
        # Upstream's documented protobuf wire fields, generated without real user data.
        def varint(value):
            result = bytearray()
            while value > 127:
                result.append((value & 127) | 128)
                value >>= 7
            result.append(value)
            return bytes(result)

        def number(field, value):
            return varint(field << 3) + varint(value)

        def blob(field, value):
            return varint((field << 3) | 2) + varint(len(value)) + value

        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        original = self.totals()
        original_projects = self.projects()
        device = self.device
        directory = self.home / ".gemini/antigravity-cli/conversations"
        directory.mkdir(parents=True)
        stamp = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).timestamp())
        writers = []
        for index in range(2):
            db = directory / f"session-{index}.db"
            writer = sqlite3.connect(db)
            writers.append(writer)
            self.addCleanup(writer.close)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.executescript("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER); CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB);")
            usage = (number(1, 1132) + number(2, 500) + number(5, 16000) +
                     number(9, 40) + number(10, 300) + blob(11, f"response-{index}".encode()))
            fake_secret = b'{"access_token":"fixture-not-a-real-token"}'
            row = blob(1, blob(2, blob(3, fake_secret)) + blob(4, usage) + blob(19, b"gemini-3-flash-a"))
            meta = blob(1, blob(1, b"file:///home/agent/workspace/Antigravity-Project")) + blob(2, number(1, stamp))
            writer.execute("INSERT INTO gen_metadata VALUES (0, ?, 0)", (row,))
            writer.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (meta,))
            if index == 1:
                duplicate = usage.replace(b"response-1", b"response-0")
                writer.execute("INSERT INTO gen_metadata VALUES (1, ?, 0)",
                               (blob(1, blob(4, duplicate) + blob(19, b"gemini-3-flash-a")),))
            writer.commit()
        self.monitor.config["providers"].append("antigravity")
        self.publish()
        self.assertIsNone(self.monitor._sanitizer_name)
        removed = subprocess.run(["docker", "container", "inspect", self.monitor.container_name + "-sanitize"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        self.assertNotEqual(removed.returncode, 0)
        for path in self.monitor.pending()[0].rglob("*"):
            if path.is_file():
                self.assertNotIn(fake_secret, path.read_bytes())
        self.monitor.deliver(self.monitor.pending()[0])
        for path in self.state.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(fake_secret, path.read_bytes())
        counts, costs = self.totals()
        self.assertEqual(counts, {**original[0], "antigravity": 33680})
        # Calculate independently from the actual image's pinned pricing
        # catalog; the source records have non-cached input, cache read,
        # response output and reasoning output as separate components.
        catalog = json.loads(subprocess.check_output([
            "docker", "run", "--rm", "--network=none", "--entrypoint", "cat", IMAGE,
            "/opt/tokscale-pricing/cache/pricing-models-dev.json",
        ], timeout=20))["data"]["google/gemini-3.5-flash"]
        expected_cost = (1000 * catalog["input_cost_per_token"] +
                         32000 * catalog["cache_read_input_token_cost"] +
                         680 * catalog["output_cost_per_token"])
        self.assertAlmostEqual(costs["antigravity"], expected_cost, places=8)
        period = self.device_record()["periods"]["allTime"]
        self.assertEqual(period["clientModels"]["antigravity"], {"gemini-3.5-flash-high": 33680})
        self.assertEqual(period["clientCacheReads"]["antigravity"], 32000)
        self.assertEqual(self.device, device)
        self.assertEqual({k: self.projects()[k] for k in original_projects}, original_projects)
        self.assertEqual(self.projects()["antigravity-project"]["clients"], {"antigravity": 33680})
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), (counts, costs))
        if rewrite:
            updated_usage = (number(1, 1132) + number(2, 700) + number(5, 16000) +
                             number(9, 40) + number(10, 300) + blob(11, b"response-1"))
            updated_row = blob(1, blob(2, blob(3, fake_secret)) + blob(4, updated_usage) +
                               blob(19, b"gemini-3-flash-a"))
            writers[1].execute("UPDATE gen_metadata SET data=? WHERE idx=0", (updated_row,))
            writers[1].commit()
            self.publish()
            self.monitor.deliver(self.monitor.pending()[0])
            advanced_counts, advanced_costs = self.totals()
            self.assertEqual(advanced_counts, {**counts, "antigravity": 33880})
            self.assertGreater(advanced_costs["antigravity"], costs["antigravity"])
            writers[1].execute("UPDATE gen_metadata SET data=? WHERE idx=0", (row,))
            writers[1].commit()
            self.publish()
            self.monitor.deliver(self.monitor.pending()[0])
            self.assertEqual(self.totals(), (advanced_counts, advanced_costs))
            counts, costs = advanced_counts, advanced_costs
        if remove_one:
            writers[0].close()
            first_db = directory / "session-0.db"
            moved = directory.parent / "removed.db"
            first_db.rename(moved)
            self.publish()
            self.monitor.deliver(self.monitor.pending()[0])
            self.assertEqual(self.totals(), (counts, costs))
            moved.rename(first_db)
            self.publish()
            self.monitor.deliver(self.monitor.pending()[0])
            self.assertEqual(self.totals(), (counts, costs))
            compact = self.monitor.collector_state / "antigravity-usage"
            backup = compact.rename(compact.with_name("usage-backup"))
            self.publish()
            with self.assertRaisesRegex(manager.JobError, "usage archive is missing"):
                self.monitor.deliver(self.monitor.pending()[0])
            self.assertEqual(self.totals(), (counts, costs))
            backup.rename(compact)
            self.monitor.deliver(self.monitor.pending()[0])
            self.assertEqual(self.totals(), (counts, costs))
            retained_file = compact / "session-0.db"
            removed_file = retained_file.rename(self.monitor.collector_state / "removed-usage.db")
            self.publish()
            with self.assertRaisesRegex(manager.JobError, "usage archive is missing"):
                self.monitor.deliver(self.monitor.pending()[0])
            self.assertEqual(self.totals(), (counts, costs))
            removed_file.rename(retained_file)
            self.monitor.deliver(self.monitor.pending()[0])
            self.assertEqual(self.totals(), (counts, costs))
        saved = directory.rename(directory.with_name("saved"))
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), (counts, costs))
        saved.rename(directory)
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), (counts, costs))

    def test_antigravity_explicit_orchestrator_and_worker_models(self):
        def varint(value):
            data = bytearray()
            while value > 127:
                data.append((value & 127) | 128)
                value >>= 7
            data.append(value)
            return bytes(data)

        def number(field, value):
            return varint(field << 3) + varint(value)

        def blob(field, value):
            return varint((field << 3) | 2) + varint(len(value)) + value

        directory = self.home / ".gemini/antigravity-cli/conversations"
        directory.mkdir(parents=True)
        for index, model in enumerate(("gemini-pro-orchestrator-neon", "gemini-flash-worker-agy")):
            with closing(sqlite3.connect(directory / f"route-{index}.db")) as db, db:
                db.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB, size INTEGER)")
                usage = number(2, 100) + number(9, 5) + number(10, 10) + blob(11, f"route-{index}".encode())
                generation = blob(1, blob(4, usage) + blob(19, model.encode()))
                db.execute("INSERT INTO gen_metadata VALUES (0, ?, 0)", (generation,))
        self.monitor.config["providers"].append("antigravity")
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        period = self.device_record()["periods"]["allTime"]
        self.assertEqual(period["clientModels"]["antigravity"], {
            "gemini-pro-orchestrator-neon": 115,
            "gemini-flash-worker-agy": 115,
        })
        self.assertEqual(period["clients"]["antigravity"], 230)
        catalog = json.loads(subprocess.check_output([
            "docker", "run", "--rm", "--network=none", "--entrypoint", "cat", IMAGE,
            "/opt/tokscale-pricing/cache/pricing-models-dev.json",
        ], timeout=20))["data"]
        for route, model in (("gemini-pro-orchestrator-neon", "google/gemini-4-neon"),
                             ("gemini-flash-worker-agy", "google/gemini-4-hydrogen")):
            rates = catalog[model]
            cost = 100 * rates["input_cost_per_token"] + 15 * rates["output_cost_per_token"]
            self.assertAlmostEqual(period["clientModelCosts"]["antigravity"][route], cost, places=8)

    def test_project_attribution_and_reconciliation_by_provider(self):
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        record = self.device_record()
        expected = {
            "claude-project": {"claude": 110},
            "codex-project": {"codex": 220},
            "opencode-current": {"opencode": 88},
            "opencode-legacy": {"opencode": 44},
        }
        self.assertIs(record["projectsEnabled"], True)
        period = record["periods"]["allTime"]
        self.assertEqual({key: value["clients"] for key, value in period["projects"].items()}, expected)
        for name, window in record["periods"].items():
            with self.subTest(period=name):
                projects = window["projects"].values()
                self.assertEqual(sum(row["tokens"] for row in projects), window["totalTokens"])
                self.assertAlmostEqual(sum(row["costUsd"] for row in projects), window["costUsd"], places=9)
        self.assertFalse(record.get("allTimeProjectsOmitted", False))

        self.monitor.config["projects_enabled"] = False
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertIs(self.device_record()["projectsEnabled"], False)
        self.assertEqual(self.projects(), {})
        self.assertEqual(self.device_record()["periods"]["allTime"]["totalTokens"], period["totalTokens"])

        self.monitor.config["projects_enabled"] = True
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.projects(), period["projects"])

    def test_project_aggregation_matches_native_labels_across_device_paths(self):
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])

        claude = self.sources[0]
        claude.unlink()
        # Distinct sessions on different devices share a project folder label.
        record = {"type": "assistant", "sessionId": "second-device", "requestId": "second-request",
                  "cwd": "/different/checkout/claude-project",
                  "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "message": {"id": "second-message", "model": "claude-sonnet-4-5",
                              "usage": {"input_tokens": 15, "output_tokens": 5}}}
        (claude.parent / "second-device.jsonl").write_text(json.dumps(record) + "\n")
        peer_state = self.path / "peer"
        peer_state.mkdir()
        with patch.object(manager, "STATE", peer_state):
            peer = manager.Monitor({**self.monitor.config, "providers": ["claude"]})
            peer.image = self.monitor.image
            peer.container_name = self.monitor.container_name
            peer.provision()
            fd = os.open(self.source, DIR_FLAGS)
            try:
                peer.publish(fd, "peer-workspace")
            finally:
                os.close(fd)
            peer.deliver(peer.pending()[0])
        status, body = self.hub.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        project = json.loads(body)["periods"]["allTime"]["projects"]["claude-project"]
        self.assertEqual(project["clients"], {"claude": 130})

    def test_unresolved_or_collector_paths_are_unattributed(self):
        claude = self.sources[0]
        record = json.loads(claude.read_text())
        for index, location in enumerate(("/snapshot/local-stage", "/opt/token-monitor/runtime",
                                          "/work/home/backing-dir", "relative/path", "/")):
            key = f"unresolved-{index}"
            record.update(cwd=location, sessionId=key, requestId=key)
            record["message"]["id"] = key
            (claude.parent / f"{key}.jsonl").write_text(json.dumps(record) + "\n")
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals()[0]["claude"], 660)
        self.assertEqual(set(self.projects()), {"claude-project", "codex-project", "opencode-current", "opencode-legacy"})
        self.assertEqual(self.projects()["claude-project"]["tokens"], 110)

    def test_historical_import_outage_recreation_restore_and_hub_restart(self):
        self.monitor.provision()
        self.monitor.provision()
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        initial = self.totals()
        initial_projects = self.projects()
        counts, costs = initial
        self.assertEqual(counts, {"claude": 110, "codex": 220, "opencode": 132})
        self.assertEqual(set(costs), set(counts))
        self.assertTrue(all(cost > 0 for cost in costs.values()))

        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), initial)

        self.stop_hub()
        self.publish()
        pending, = self.monitor.pending()
        with patch.object(manager, "workspace", return_value=None):
            with self.assertRaises(manager.JobError):
                self.monitor.sync(scheduled=True)
        self.assertTrue(pending.is_dir())
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["pending"], 1)
        self.assertGreater(status["retry_after"], time.time())

        self.start_hub()
        self.assertEqual(self.totals(), initial)
        with patch.object(manager, "workspace", return_value=None):
            self.monitor.sync(scheduled=True)
            self.assertTrue(pending.is_dir())
            with patch.object(manager.time, "time", return_value=status["retry_after"] + 1):
                self.monitor.sync(scheduled=True)
        self.assertFalse(pending.is_dir())
        self.assertEqual(self.totals(), initial)

        restored = self.home.rename(self.path / "restored")
        self.home.mkdir()
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), initial)
        self.home.rmdir()
        restored.rename(self.home)
        claude = self.sources[0]
        row = json.loads(claude.read_text())
        row["cwd"] = "/restored/different-folder-name"
        claude.write_text(json.dumps(row) + "\n")
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), initial)
        self.assertEqual(self.projects(), initial_projects)

    def test_older_backup_keeps_newer_session_versions_and_imports_new_usage(self):
        self.check_older_backup(age_days=5)

    def test_older_backup_beyond_native_daily_scan_does_not_add_a_false_day(self):
        self.check_older_backup(age_days=45)

    def test_resuming_a_shorter_backup_does_not_reduce_historical_totals(self):
        self.check_older_backup(age_days=-1)

    def check_older_backup(self, age_days):
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        original_counts, original_costs = self.totals()

        claude, codex, _, db = self.sources
        record = json.loads(claude.read_text())
        newest = datetime.datetime.fromisoformat(record["timestamp"])
        older = newest - datetime.timedelta(days=age_days)
        record["timestamp"] = older.isoformat()
        record["message"]["usage"]["input_tokens"] = 20
        claude.write_text(json.dumps(record) + "\n")

        # An unrelated session in the same restored source remains visible.
        record.update(sessionId="new-session", requestId="new-request", timestamp=newest.isoformat())
        record["message"]["id"] = "new-message"
        (claude.parent / "new-session.jsonl").write_text(json.dumps(record) + "\n")

        rows = [json.loads(line) for line in codex.read_text().splitlines()]
        for row in rows:
            row["timestamp"] = older.isoformat()
        payload = rows[-1]["payload"]["info"]
        for usage in payload.values():
            usage.update(input_tokens=50, cached_input_tokens=0, total_tokens=70)
        codex.write_text("".join(json.dumps(row) + "\n" for row in rows))

        with closing(sqlite3.connect(db)) as writer, writer:
            second = json.loads(writer.execute("SELECT data FROM session_message").fetchone()[0])
            second["time"] = {"created": int(older.timestamp() * 1000)}
            second["tokens"]["input"] = 12
            writer.execute("UPDATE session_message SET data = ?", (json.dumps(second),))

        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        expected = {**original_counts, "claude": 140}
        counts, costs = self.totals()
        self.assertEqual(counts, expected)
        projects = self.projects()
        self.assertEqual(sum(row["tokens"] for row in projects.values()), sum(expected.values()))
        self.assertAlmostEqual(sum(row["costUsd"] for row in projects.values()), sum(costs.values()), places=9)
        self.assertGreater(costs["claude"], original_costs["claude"])
        self.assertEqual(costs.get("opencode", 0), original_costs.get("opencode", 0))
        self.assertEqual(costs.get("codex", 0), original_costs.get("codex", 0))
        status, body = self.hub.request("GET", "/api/history")
        self.assertEqual(status, 200)
        daily, = json.loads(body)["daily"]
        self.assertEqual(daily["date"], newest.date().isoformat())
        self.assertEqual(daily["tokens"], sum(expected.values()))
        self.assertAlmostEqual(daily["cost"], sum(costs.values()), places=6)

        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), (counts, costs))

    def test_adopting_an_existing_device_preserves_deleted_sessions(self):
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        prior_totals = self.totals()
        prior_projects = self.projects()

        # Move only the usage archives, as the handoff guide prescribes.
        prior_archive = self.monitor.collector_state / "archive"
        adopted_state = self.path / "adopted"
        archive = adopted_state / "collector/archive"
        archive.mkdir(parents=True)
        for name in ("session-usage-archive.sqlite", "daily-history-calendars.json"):
            source = prior_archive / name
            if source.is_file():
                shutil.copyfile(source, archive / name)
        self.home.rename(self.path / "old-home")
        self.home.mkdir()

        with patch.object(manager, "STATE", adopted_state):
            adopted = manager.Monitor({**self.monitor.config, "device": self.device})
            adopted.image = self.monitor.image
            adopted.container_name = self.monitor.container_name
            adopted.snapshots.mkdir()
            fd = os.open(self.source, DIR_FLAGS)
            try:
                adopted.publish(fd, "adopted-fixture")
            finally:
                os.close(fd)
            adopted.deliver(adopted.pending()[0])
        self.assertEqual(self.totals(), prior_totals)
        self.assertEqual(self.projects(), prior_projects)

    @unittest.skipUnless(shutil.which("zstd"), "requires zstd to generate a Codex archive fixture")
    def test_compressed_codex_archive_and_jsonl_tails(self):
        claude, codex, _, _ = self.sources
        # Both a complete final record without LF and an incomplete fragment
        # should reach the pinned scanner with their source bytes intact.
        claude.write_bytes(claude.read_bytes().rstrip(b"\n"))
        archive = self.home / ".codex/archived_sessions" / (codex.name + ".zst")
        archive.parent.mkdir()
        (claude.parent / "incomplete.jsonl").write_bytes(b'{"type":"assistant"')
        subprocess.run(["zstd", "-q", "-o", str(archive)], input=codex.read_bytes(),
                       check=True, timeout=10)
        codex.unlink()

        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals()[0], {"claude": 110, "codex": 220, "opencode": 132})

    def test_setup_upgrade_preserves_running_workspace_and_three_provider_history(self):
        def number(field, value):
            return antigravity_projection.enc(field << 3) + antigravity_projection.enc(value)

        def blob(field, value):
            return (antigravity_projection.enc((field << 3) | 2) +
                    antigravity_projection.enc(len(value)) + value)

        project = self.path / "upgrade-project"
        project.mkdir()
        shutil.copytree(ROOT / "scripts/token-monitor", project / "scripts/token-monitor")
        units = self.path / "systemd"
        units.mkdir()
        secret_file = project / ".token-monitor.secret"
        secret_file.write_text(self.secret + "\n")
        secret_file.chmod(0o600)
        config_file = project / ".token-monitor.json"
        config = {"enabled": True, "hub_url": f"http://127.0.0.1:{self.port}",
                  "secret_file": ".token-monitor.secret", "providers": ["claude", "codex", "opencode"],
                  "projects_enabled": True, "timezone": "UTC"}
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        agy_dir = self.home / ".gemini/antigravity-cli/conversations"
        agy_dir.mkdir(parents=True)
        stamp = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).timestamp())
        usage = number(2, 100) + number(10, 10) + blob(11, b"upgrade-response")
        row = blob(1, blob(4, usage) + blob(19, b"gemini-3-flash-a"))
        meta = blob(1, blob(1, b"file:///home/agent/workspace/Agy-Project")) + blob(2, number(1, stamp))
        with closing(sqlite3.connect(agy_dir / "upgrade.db")) as db, db:
            db.executescript("CREATE TABLE gen_metadata(idx INTEGER,data BLOB); CREATE TABLE trajectory_metadata_blob(data BLOB);")
            db.execute("INSERT INTO gen_metadata VALUES (0,?)", (row,))
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (meta,))
        fixture = self.path / "upgrade-fixture"
        shutil.copytree(self.home, fixture / "home/agent")
        (fixture / "Dockerfile").write_text(
            f"FROM {IMAGE}\nCOPY --chown={os.getuid()}:{os.getgid()} home/agent/ /home/agent/\n")
        image = f"{self.monitor.container_name}-upgrade-workspace"
        self.addCleanup(subprocess.run, ["docker", "image", "rm", image],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "build", "--quiet", "-t", image, str(fixture)],
                       check=True, stdout=subprocess.DEVNULL, timeout=60)
        cid = subprocess.check_output([
            "docker", "run", "-d", "--network=none", "--cap-drop=ALL", "--init",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--label", "com.docker.compose.project=agent-sandbox",
            "--label", "com.docker.compose.service=workspace",
            "--label", f"com.docker.compose.project.working_dir={project}",
            "--entrypoint", "sh", image, "-c",
            "printf active > /home/agent/agent-marker; exec sleep infinity",
        ], text=True, timeout=15).strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", cid],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "exec", cid, "sh", "-c", "while test ! -f /home/agent/agent-marker; do sleep 0.1; done"],
                       check=True, timeout=10)
        def state_of_workspace():
            return subprocess.check_output(["docker", "inspect", "--format",
                                            "{{.State.Pid}}|{{.State.StartedAt}}|{{.State.Running}}", cid],
                                           text=True, timeout=10).strip()
        before_workspace = state_of_workspace()
        original_init = manager.Monitor.__init__
        def test_init(monitor, settings):
            original_init(monitor, settings)
            monitor.image = IMAGE
            monitor.container_name = self.monitor.container_name
        saved_umask = os.umask(0o077)
        self.addCleanup(os.umask, saved_umask)
        with patch.multiple(manager, PROJECT=project, CONFIG=config_file, SYSTEMD=units), \
             patch.object(manager.os, "geteuid", return_value=0), \
             patch.object(manager, "private_state"), patch.object(manager, "systemctl") as systemctl, \
             patch.object(manager.Monitor, "__init__", test_init), patch.object(sys, "argv", ["manager", "setup"]):
            self.assertEqual(manager.main(), 0)
            before_totals = self.totals()
            before_projects = self.projects()
            self.assertEqual(before_totals[0], {"claude": 110, "codex": 220, "opencode": 132})
            before_device = (self.state / "device.json").read_bytes()
            before_secret = secret_file.read_bytes()
            before_units = {path.name: (path.read_bytes(), path.stat().st_ino) for path in units.iterdir()}
            self.assertEqual(len(before_units), 2)
            # Previously captured history must survive when its source is gone.
            subprocess.run(["docker", "exec", cid, "rm", "-rf", "/home/agent/.claude/projects"], check=True, timeout=10)
            config["providers"].append("antigravity")
            config_file.write_text(json.dumps(config))
            self.assertEqual(manager.main(), 0)
            self.assertEqual(state_of_workspace(), before_workspace)
            self.assertEqual(subprocess.check_output(["docker", "exec", cid, "cat", "/home/agent/agent-marker"], text=True), "active")
            self.assertEqual((self.state / "device.json").read_bytes(), before_device)
            self.assertEqual(secret_file.read_bytes(), before_secret)
            self.assertEqual({path.name: (path.read_bytes(), path.stat().st_ino) for path in units.iterdir()}, before_units)
            self.assertEqual(json.loads(config_file.read_text()), config)
            self.assertEqual(self.totals()[0], {**before_totals[0], "antigravity": 110})
            self.assertEqual({key: self.projects()[key] for key in before_projects}, before_projects)
            self.assertEqual(self.projects()["agy-project"]["clients"], {"antigravity": 110})
            self.assertTrue(systemctl.called)

    def test_antigravity_final_capture_and_empty_recreation(self):
        self.check_antigravity_final_capture(outage=False)

    def test_antigravity_final_capture_recovers_after_hub_outage(self):
        self.check_antigravity_final_capture(outage=True)

    def check_antigravity_final_capture(self, outage):
        def number(field, value):
            return antigravity_projection.enc(field << 3) + antigravity_projection.enc(value)

        def blob(field, value):
            return (antigravity_projection.enc((field << 3) | 2) +
                    antigravity_projection.enc(len(value)) + value)

        secret = b'{"access_token":"fixture-private-final-value"}'
        def generation(identifier, inputs, outputs):
            usage = number(2, inputs) + number(10, outputs) + blob(11, identifier)
            return blob(1, blob(2, blob(3, secret)) + blob(4, usage) + blob(19, b"gemini-3-flash-a"))

        project = self.path / "workspace-project"
        project.mkdir()
        fixture = self.path / "final-fixture"
        conversations = fixture / ".gemini/antigravity-cli/conversations"
        conversations.mkdir(parents=True)
        db_path = conversations / "final-session.db"
        stamp = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).timestamp())
        meta = blob(1, blob(1, b"file:///home/agent/workspace/Final-Project")) + blob(2, number(1, stamp))
        with closing(sqlite3.connect(db_path)) as db, db:
            db.executescript("CREATE TABLE gen_metadata(idx INTEGER, data BLOB); CREATE TABLE trajectory_metadata_blob(data BLOB);")
            db.execute("INSERT INTO gen_metadata VALUES (0, ?)", (generation(b"initial", 100, 10),))
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (meta,))
        (fixture / "Dockerfile").write_text(
            f"FROM {IMAGE}\n"
            f"COPY --chown={os.getuid()}:{os.getgid()} .gemini /home/agent/.gemini\n"
        )
        image = f"{self.monitor.container_name}-agy-workspace"
        self.addCleanup(subprocess.run, ["docker", "image", "rm", image],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "build", "--quiet", "-t", image, str(fixture)],
                       check=True, stdout=subprocess.DEVNULL, timeout=60)
        saved_project, manager.PROJECT = manager.PROJECT, project
        self.addCleanup(setattr, manager, "PROJECT", saved_project)
        self.monitor.config["providers"] = ["antigravity"]
        script = """import os, signal, sqlite3, time
path = '/home/agent/.gemini/antigravity-cli/conversations/final-session.db'
db = sqlite3.connect(path)
db.execute('PRAGMA journal_mode=WAL')
def finish(*_):
    db.execute('INSERT INTO gen_metadata VALUES (1, ?)', (bytes.fromhex(os.environ['FINAL_ROW']),))
    db.commit()
    db.close()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, finish)
open('/tmp/fixture-ready', 'w').close()
while True: time.sleep(0.1)
"""
        cid = subprocess.check_output([
            "docker", "run", "-d", "--network=none", "--cap-drop=ALL", "--init",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--label", "com.docker.compose.project=agent-sandbox",
            "--label", "com.docker.compose.service=workspace",
            "--label", f"com.docker.compose.project.working_dir={project}",
            "--env", f"FINAL_ROW={generation(b'final', 50, 5).hex()}",
            "--entrypoint", "/usr/bin/python3", image, "-c", script,
        ], text=True, timeout=15).strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", cid],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "exec", cid, "sh", "-c", "while test ! -e /tmp/fixture-ready; do sleep 0.1; done"],
                       check=True, timeout=10)
        self.monitor.sync()
        self.assertEqual(self.totals()[0], {"antigravity": 110})
        if outage:
            self.stop_hub()
        self.monitor.sync(final=True)
        self.assertEqual(manager.workspace()[1], 0)
        if outage:
            self.assertEqual(len(self.monitor.pending()), 1)
        for path in self.state.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(secret, path.read_bytes())
        subprocess.run(["docker", "rm", cid], check=True, stdout=subprocess.DEVNULL)
        if outage:
            self.start_hub()
            self.monitor.sync()
            self.assertEqual(self.monitor.pending(), [])
        self.assertEqual(self.totals()[0], {"antigravity": 165})
        self.assertEqual(self.projects()["final-project"]["tokens"], 165)
        recreated = subprocess.check_output([
            "docker", "run", "-d", "--network=none", "--cap-drop=ALL",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--label", "com.docker.compose.project=agent-sandbox",
            "--label", "com.docker.compose.service=workspace",
            "--label", f"com.docker.compose.project.working_dir={project}",
            "--entrypoint", "sh", image, "-c",
            "rm -rf /home/agent/.gemini/antigravity-cli/conversations; exec sleep infinity",
        ], text=True, timeout=15).strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", recreated],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "exec", recreated, "sh", "-c",
                        "while test -e /home/agent/.gemini/antigravity-cli/conversations; do sleep 0.1; done"],
                       check=True, timeout=10)
        with patch.object(manager, "PROJECT", ROOT):
            restarted = manager.Monitor(self.monitor.config)
        restarted.image = IMAGE
        restarted.container_name = self.monitor.container_name
        restarted.sync()
        self.assertEqual(self.totals()[0], {"antigravity": 165})
        self.assertEqual(self.projects()["final-project"]["tokens"], 165)

    def test_controlled_stop_captures_last_write_before_container_removal(self):
        # Build an isolated workspace with fixture data in its writable-overlay
        # ancestry. No live sandbox, bridge, firewall, or host mount is used.
        project = self.path / "project"
        project.mkdir()
        fixture = self.path / "fixture"
        shutil.copytree(self.home / ".claude", fixture / ".claude")
        (fixture / "Dockerfile").write_text(
            f"FROM {IMAGE}\n"
            f"COPY --chown={os.getuid()}:{os.getgid()} .claude /home/agent/.claude\n"
        )
        image = f"{self.monitor.container_name}-workspace"
        self.addCleanup(subprocess.run, ["docker", "image", "rm", image],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "build", "--quiet", "-t", image, str(fixture)],
                       check=True, stdout=subprocess.DEVNULL, timeout=60)
        saved_project, manager.PROJECT = manager.PROJECT, project
        self.addCleanup(setattr, manager, "PROJECT", saved_project)
        session = self.sources[0].relative_to(self.home)
        record = json.loads(self.sources[0].read_text())
        record["message"]["usage"]["input_tokens"] = 50
        record["message"]["id"] = "final-message"
        record["requestId"] = "final-request"
        cid = subprocess.check_output([
            "docker", "run", "-d", "--network=none", "--cap-drop=ALL", "--init",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--label", "com.docker.compose.project=agent-sandbox",
            "--label", "com.docker.compose.service=workspace",
            "--label", f"com.docker.compose.project.working_dir={project}",
            "--env", f"FINAL_RECORD={json.dumps(record)}", "--env", f"FINAL_PATH=/home/agent/{session}",
            "--entrypoint", "sh", image, "-c",
            """trap 'printf "%s\\n" "$FINAL_RECORD" >> "$FINAL_PATH"; exit' TERM
while :; do sleep 0.1; done""",
        ], text=True, timeout=15).strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", cid],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.monitor.sync()
        self.assertEqual(self.totals()[0], {"claude": 110})
        self.assertEqual(self.projects()["claude-project"]["tokens"], 110)
        self.monitor.sync(final=True)
        self.assertEqual(manager.workspace()[1], 0)
        self.assertEqual(self.totals()[0], {"claude": 170})
        subprocess.run(["docker", "rm", cid], check=True, stdout=subprocess.DEVNULL)
        self.assertIsNone(manager.workspace())
        self.assertEqual(self.totals()[0], {"claude": 170})
        self.assertEqual(self.projects()["claude-project"]["tokens"], 170)

        recreated = subprocess.check_output([
            "docker", "run", "-d", "--network=none", "--cap-drop=ALL",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--label", "com.docker.compose.project=agent-sandbox",
            "--label", "com.docker.compose.service=workspace",
            "--label", f"com.docker.compose.project.working_dir={project}",
            "--entrypoint", "sh", image, "-c",
            "rm -rf /home/agent/.claude/projects; exec sleep infinity",
        ], text=True, timeout=15).strip()
        self.addCleanup(subprocess.run, ["docker", "rm", "-f", recreated],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "exec", recreated, "sh", "-c",
                        "while test -e /home/agent/.claude/projects; do sleep 0.1; done"],
                       check=True, timeout=10)
        config = self.monitor.config
        # A fresh process uses only the persisted device and scanner state.
        with patch.object(manager, "PROJECT", ROOT):
            restarted = manager.Monitor(config)
        restarted.image = IMAGE
        restarted.container_name = self.monitor.container_name
        restarted.sync()
        self.assertEqual(self.totals()[0], {"claude": 170})
        self.assertEqual(self.projects()["claude-project"]["tokens"], 170)


if __name__ == "__main__":
    unittest.main()
