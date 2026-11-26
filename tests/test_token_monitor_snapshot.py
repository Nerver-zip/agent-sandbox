from contextlib import closing
import importlib.util
import io
import subprocess
import tarfile
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
        result = snapshot.capture(self.fd, target, previous, providers, self.sanitize_fixture)
        return target, result

    def sanitize_fixture(self, companions, target):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as stream:
            for index, (_, fd, info) in enumerate(companions):
                if fd is None:
                    continue
                member = tarfile.TarInfo(("usage.db", "usage.db-wal", "usage.db-journal")[index])
                member.size = info[2]
                os.lseek(fd, 0, os.SEEK_SET)
                with os.fdopen(os.dup(fd), "rb") as source:
                    stream.addfile(member, source)
        with tempfile.TemporaryDirectory() as work, target.open("xb") as output:
            subprocess.run(["python3", "-I", str(MODULE.parent / "antigravity_sanitize.py"), work],
                           input=data.getvalue(), stdout=output, stderr=subprocess.DEVNULL, check=True)

    def test_only_supported_sources_keep_bytes_and_source_time(self):
        data = {
            ".claude/projects/project/a.jsonl": b"{}\n{",
            ".codex/sessions/2026/11/01/rollout.jsonl": b"{}\n",
            ".codex/archived_sessions/rollout.jsonl": b"{}",
            ".codex/archived_sessions/compressed.jsonl.zst": b"compressed fixture",
            ".local/share/opencode/storage/message/session/msg.json": b"{}",
        }
        stamp = 1782806400123456789
        for path, content in data.items():
            target = self.write(path, content)
            os.utime(target, ns=(stamp, stamp))
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
        self.assertEqual(selected, data)
        for path in data:
            self.assertEqual((target / "home" / path).stat().st_mtime_ns, stamp)

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
        with closing(sqlite3.connect(first / "home" / snapshot.OPENCODE / "opencode.db")) as reader:
            self.assertEqual(reader.execute("select value from message").fetchall(), [(1,)])
        writer.rollback()

    def test_antigravity_fails_closed_without_sanitizer(self):
        self.write(f"{snapshot.ANTIGRAVITY}/conversation.db", b"raw fixture")
        destination = self.tmp / "unsafe"
        with self.assertRaisesRegex(snapshot.SnapshotError, "isolated metadata sanitizer"):
            snapshot.capture(self.fd, destination, None, ["antigravity"])
        self.assertFalse(destination.exists())

    def test_antigravity_allowlist_and_wal_recovery(self):
        directory = self.home / snapshot.ANTIGRAVITY
        directory.mkdir(parents=True)
        db = directory / "conversation.db"
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        writer.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"\x0a\x04\x22\x02\x10\x01",))
        writer.commit()
        writer.execute("BEGIN")
        writer.execute("INSERT INTO gen_metadata VALUES (2, ?)", (b"\x0a\x04\x22\x02\x10\x02",))
        self.write(".gemini/antigravity-cli/active_session.env", b"credential")
        self.write(".gemini/antigravity-cli/conversation_summaries.db", b"not selected")
        self.write(".gemini/antigravity-cli/implicit/other.pb", b"not selected")
        self.write(".gemini/antigravity-cli/conversations/unrelated.log", b"not selected")
        first, (count, _) = self.capture(providers=["antigravity"])
        self.assertEqual(count, 1)
        paths = {str(p.relative_to(first / "home")) for p in (first / "home").rglob("*") if p.is_file()}
        self.assertEqual(paths, {f"{snapshot.ANTIGRAVITY}/conversation.db"})
        with closing(sqlite3.connect(first / "home" / snapshot.ANTIGRAVITY / db.name)) as reader:
            self.assertEqual(reader.execute("SELECT idx FROM gen_metadata").fetchall(), [(1,)])
        writer.rollback()

    def test_antigravity_reuses_projected_snapshot_when_source_is_unchanged(self):
        directory = self.home / snapshot.ANTIGRAVITY
        directory.mkdir(parents=True)
        db = directory / "session.db"
        with closing(sqlite3.connect(db)) as writer, writer:
            writer.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB)")
            writer.execute("INSERT INTO gen_metadata VALUES (0, ?)", (b"\x0a\x04\x22\x02\x10\x01",))
        first, _ = self.capture(providers=["antigravity"])
        with mock.patch.object(self, "sanitize_fixture", side_effect=AssertionError("unexpected rerun")):
            second, result = self.capture("second", first, providers=["antigravity"])
        self.assertEqual(result, (1, 0))
        self.assertTrue((first / "home" / snapshot.ANTIGRAVITY / db.name).samefile(
            second / "home" / snapshot.ANTIGRAVITY / db.name))

    def test_antigravity_write_during_capture_is_rejected_then_retried(self):
        directory = self.home / snapshot.ANTIGRAVITY
        directory.mkdir(parents=True)
        db = directory / "live.db"
        writer = sqlite3.connect(db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        writer.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"\x0a\x04\x22\x02\x10\x01",))
        writer.commit()
        original = self.sanitize_fixture
        changed = False

        def mutate_after_capture(companions, target):
            nonlocal changed
            original(companions, target)
            if not changed:
                writer.execute("INSERT INTO gen_metadata VALUES (2, ?)", (b"\x0a\x04\x22\x02\x10\x02",))
                writer.commit()
                changed = True

        with mock.patch.object(self, "sanitize_fixture", mutate_after_capture):
            with self.assertRaises(snapshot.SnapshotError):
                self.capture("interrupted", providers=["antigravity"])
        self.assertFalse((self.tmp / "interrupted").exists())
        captured, _ = self.capture("retry", providers=["antigravity"])
        with closing(sqlite3.connect(captured / "home" / snapshot.ANTIGRAVITY / "live.db")) as reader:
            self.assertEqual(reader.execute("SELECT idx FROM gen_metadata").fetchall(), [(1,), (2,)])

    def test_interrupted_capture_discards_generation(self):
        path = self.write(".codex/sessions/a.jsonl", b"{}\n")
        old, _ = self.capture()
        path.write_bytes(b'{"changed":true}\n')
        original = os.fsync

        def append_mid_copy(fd):
            original(fd)
            with path.open("ab") as source:
                source.write(b"{}\n")

        with mock.patch.object(snapshot.os, "fsync", append_mid_copy):
            with self.assertRaises(snapshot.SnapshotError):
                self.capture("interrupted", old)
        self.assertFalse((self.tmp / "interrupted").exists())
        self.assertEqual((old / "home/.codex/sessions/a.jsonl").read_bytes(), b"{}\n")


if __name__ == "__main__":
    unittest.main()
