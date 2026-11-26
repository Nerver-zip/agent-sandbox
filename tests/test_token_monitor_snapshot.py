import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / "scripts/token-monitor/snapshot.py"
SPEC = importlib.util.spec_from_file_location("snapshot", MODULE)
snapshot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snapshot)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.root = self.tmp / "root"
        self.home = self.root / "home/agent"
        self.home.mkdir(parents=True)
        self.fd = os.open(self.root, snapshot.DIR_FLAGS)
        self.addCleanup(os.close, self.fd)

    def write(self, path, content):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def capture(self, name="snapshot", previous=None, providers=snapshot.ROOTS):
        target = self.tmp / name
        result = snapshot.capture(self.fd, target, previous, providers)
        return target, result

    def test_only_supported_sources_and_complete_jsonl(self):
        data = {
            ".claude/projects/project/a.jsonl": b"{}\n{",
            ".codex/sessions/2026/11/01/rollout.jsonl": b"{}\n",
            ".codex/archived_sessions/rollout.jsonl": b"{}\n",
            ".local/share/opencode/storage/message/session/msg.json": b"{}",
        }
        for path, content in data.items():
            self.write(path, content)
        self.write(".codex/auth.json", b"test secret")
        self.write(".claude/.credentials.json", b"test secret")
        self.write(".local/share/opencode/auth.json", b"test secret")
        target, (files, copied) = self.capture()
        self.assertEqual(files, len(data))
        self.assertEqual(copied, sum(map(len, data.values())))
        selected = {
            str(path.relative_to(target / "home")): path.read_bytes()
            for path in (target / "home").rglob("*") if path.is_file()
        }
        data[".claude/projects/project/a.jsonl"] = b"{}\n"
        self.assertEqual(selected, data)

    def test_reject_links_fifos_and_mount_crossing(self):
        source = self.write(".codex/auth.json", b"test secret")
        directory = self.home / ".codex/sessions"
        directory.mkdir()
        (directory / "sym.jsonl").symlink_to(source)
        (directory / "directory").symlink_to(self.tmp)
        os.link(source, directory / "hard.jsonl")
        os.mkfifo(directory / "pipe.jsonl")
        mounted = self.write(".codex/sessions/mount/a.jsonl", b"{}\n")
        mounted_inode = mounted.parent.stat().st_ino
        original = snapshot.mount_id

        def mount_id(fd):
            if os.fstat(fd).st_ino == mounted_inode:
                return "other"
            return original(fd)

        with mock.patch.object(snapshot, "mount_id", mount_id):
            _, result = self.capture()
        self.assertEqual(result, (0, 0))

    def test_incremental_capture_reuses_unchanged_data_and_observes_deletions(self):
        retained = ".codex/sessions/retained.jsonl"
        removed = self.write(".codex/sessions/removed.jsonl", b"{}\n")
        self.write(retained, b"{}\n")
        first, _ = self.capture()
        removed.unlink()
        added = ".codex/archived_sessions/moved.jsonl"
        self.write(added, b'{"moved":true}\n')
        second, result = self.capture("second", first)
        self.assertEqual(result, (2, 15))
        self.assertTrue((first / "home" / retained).samefile(second / "home" / retained))
        self.assertTrue((first / "home/.codex/sessions/removed.jsonl").exists())
        self.assertFalse((second / "home/.codex/sessions/removed.jsonl").exists())

    def test_wal_generation_contains_committed_rows(self):
        db_path = self.home / snapshot.OPENCODE / "opencode.db"
        db_path.parent.mkdir(parents=True)
        writer = sqlite3.connect(db_path)
        self.addCleanup(writer.close)
        writer.execute("pragma journal_mode=wal")
        writer.execute("create table message (value integer)")
        writer.execute("insert into message values (1)")
        writer.commit()
        writer.execute("begin")
        writer.execute("insert into message values (2)")
        first, (count, _) = self.capture()
        self.assertEqual(count, 2)
        self.assertFalse((first / "home" / snapshot.OPENCODE / "opencode.db-shm").exists())
        # Exercise SQLite against a private, disposable copy. In production
        # the collector opens this pair inside its own process boundary.
        with sqlite3.connect(first / "home" / snapshot.OPENCODE / "opencode.db") as reader:
            self.assertEqual(reader.execute("select value from message").fetchall(), [(1,)])
        writer.rollback()

    def test_interrupted_capture_discards_generation(self):
        path = self.write(".codex/sessions/a.jsonl", b"{}\n")
        old, _ = self.capture()
        path.write_bytes(b'{"changed":true}\n')
        original = snapshot.truncate_partial_line

        def append_mid_copy(dest, size):
            original(dest, size)
            with path.open("ab") as source:
                source.write(b"{}\n")

        with mock.patch.object(snapshot, "truncate_partial_line", append_mid_copy):
            with self.assertRaises(snapshot.SnapshotError):
                self.capture("interrupted", old)
        self.assertFalse((self.tmp / "interrupted").exists())
        self.assertEqual((old / "home/.codex/sessions/a.jsonl").read_bytes(), b"{}\n")


if __name__ == "__main__":
    unittest.main()
