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
            "version": manager.DEFAULT_VERSION, "providers": ["claude", "codex", "opencode"], "timezone": "UTC",
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
            "message": {"id": "claude-message", "role": "assistant", "model": "claude-sonnet-4-5",
                        "usage": {"input_tokens": 100, "output_tokens": 10}},
        }) + "\n")
        usage = {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 20,
                 "reasoning_output_tokens": 0, "total_tokens": 220}
        codex.write_text("".join(json.dumps({"timestamp": when, **record}) + "\n" for record in [
            {"type": "session_meta", "payload": {"id": "codex-session", "cwd": "/workspace",
                                                 "originator": "codex_cli_rs", "source": "cli"}},
            {"type": "turn_context", "payload": {"turn_id": "turn", "model": "gpt-5.4"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage,
                                                                           "last_token_usage": usage}}},
        ]))
        opencode = self.home / ".local/share/opencode"
        legacy = opencode / "storage/message/ses_legacy/msg_legacy.json"
        legacy.parent.mkdir(parents=True)
        message = {"id": "msg_legacy", "sessionID": "ses_legacy", "role": "assistant", "modelID": "gpt-4o",
                   "providerID": "openai", "time": {"created": ms, "completed": ms + 1},
                   "tokens": {"input": 40, "output": 4, "reasoning": 0, "cache": {"read": 0, "write": 0}}}
        legacy.write_text(json.dumps(message))
        db = opencode / "opencode.db"
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.executescript("""
            PRAGMA journal_mode=wal;
            CREATE TABLE session_v2(id TEXT PRIMARY KEY, directory TEXT, title TEXT);
            CREATE TABLE session_message(id TEXT PRIMARY KEY, session_id TEXT, type TEXT, data TEXT);
            INSERT INTO session_v2 VALUES ('ses_current', '/workspace', 'private fixture title');
        """)
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

    def totals(self):
        status, body = self.hub.request("GET", "/api/devices")
        self.assertEqual(status, 200)
        record, = json.loads(body)["devices"]
        self.assertEqual(record["deviceId"], self.device)
        self.assertNotIn("private fixture title", body.decode())
        period = record["periods"]["allTime"]
        return period["clients"], period["clientCosts"]

    def test_historical_import_outage_recreation_restore_and_hub_restart(self):
        self.monitor.provision()
        self.monitor.provision()
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        initial = self.totals()
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
        self.publish()
        self.monitor.deliver(self.monitor.pending()[0])
        self.assertEqual(self.totals(), initial)

    def test_older_backup_keeps_newer_session_versions_and_imports_new_usage(self):
        self.check_older_backup(age_days=5)

    def test_older_backup_beyond_native_daily_scan_does_not_add_a_false_day(self):
        self.check_older_backup(age_days=45)

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
        self.monitor.sync(final=True)
        self.assertEqual(manager.workspace()[1], 0)
        self.assertEqual(self.totals()[0], {"claude": 170})
        subprocess.run(["docker", "rm", cid], check=True, stdout=subprocess.DEVNULL)
        self.assertIsNone(manager.workspace())
        self.assertEqual(self.totals()[0], {"claude": 170})

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


if __name__ == "__main__":
    unittest.main()
