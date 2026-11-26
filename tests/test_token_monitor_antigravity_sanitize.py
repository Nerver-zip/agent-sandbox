from contextlib import closing
import io
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts/token-monitor/antigravity_sanitize.py"


def archive(files):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as stream:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
    return result.getvalue()


class SanitizeTest(unittest.TestCase):
    def test_private_copy_recovers_wal_and_projects_only_allowed_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            secret = b'{"access_token":"fixture-private-value"}'
            with closing(sqlite3.connect(source)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE gen_metadata(idx INTEGER,data BLOB)")
                # root #1 / chatModel #2 is not consumed by the pinned scanner.
                message = b"\x0a" + bytes([len(secret)]) + secret
                chat = b"\x12" + bytes([len(message)]) + message + b"\x22\x02\x10\x0f"
                row = b"\x0a" + bytes([len(chat)]) + chat
                writer.execute("INSERT INTO gen_metadata VALUES (0, ?)", (row,))
                writer.commit()
                writer.execute("BEGIN")
                writer.execute("INSERT INTO gen_metadata VALUES (1, ?)", (row,))
                raw = archive([(name, path.read_bytes()) for name, path in [
                    ("usage.db", source), ("usage.db-wal", source.with_name(source.name + "-wal"))]])
                result = subprocess.run(["python3", "-I", str(WORKER), str(root / "work")], input=raw,
                                        capture_output=True, timeout=15, check=True)
                self.assertNotIn(secret, result.stdout)
                self.assertEqual(list((root / "work").iterdir()), [])
                output = root / "result.db"
                output.write_bytes(result.stdout)
                with closing(sqlite3.connect(output)) as reader:
                    rows = reader.execute("SELECT data FROM gen_metadata").fetchall()
                self.assertEqual(rows, [(b"\x0a\x04\x22\x02\x10\x0f",)])
                writer.rollback()

    def test_rejects_unexpected_tar_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            data = archive([("../escape", b"fixture")])
            result = subprocess.run(["python3", "-I", str(WORKER), directory], input=data,
                                    capture_output=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
