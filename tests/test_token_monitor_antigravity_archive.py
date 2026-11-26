from contextlib import closing
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "scripts/token-monitor/antigravity_archive.py"
SPEC = importlib.util.spec_from_file_location("antigravity_archive", MODULE)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


class AntigravityArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.retained = self.root / "archive"
        self.source.mkdir()
        self.db = self.source / "session.db"
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("CREATE TABLE gen_metadata(idx INTEGER, data BLOB); CREATE TABLE trajectory_metadata_blob(data BLOB); CREATE TABLE steps(idx INTEGER, step_type INTEGER, metadata BLOB, content TEXT); CREATE TABLE credentials(secret TEXT);")
            db.execute("INSERT INTO gen_metadata VALUES (0, ?)", (b"first",))
            db.execute("INSERT INTO credentials VALUES (?)", ("fixture secret",))

    def read_rows(self):
        with closing(sqlite3.connect(self.retained / self.db.name)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            self.assertEqual(tables, set(archive.TABLES))
            self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(steps)")], ["idx", "step_type", "metadata"])
            return db.execute("SELECT idx,data FROM gen_metadata ORDER BY idx").fetchall()

    def test_retention_update_restore_and_exclusion(self):
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first")])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"second",))
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first"), (1, b"second")])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM gen_metadata WHERE idx=1")
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first"), (1, b"second")])
        self.db.unlink()
        self.assertEqual(len(archive.update(self.source, self.retained)), 1)
        self.assertEqual(self.read_rows(), [(0, b"first"), (1, b"second")])

    def test_divergent_versions_fail_without_replacing_archive(self):
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (b"different",))
        with self.assertRaisesRegex(RuntimeError, "Divergent"):
            archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first")])
        self.assertFalse(list(self.retained.glob(".usage-*")))

    def test_interrupted_candidate_is_discarded_on_next_run(self):
        self.retained.mkdir()
        (self.retained / ".usage-abandoned").write_bytes(b"partial")
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first")])
        self.assertFalse(list(self.retained.glob(".usage-*")))

    def test_failed_atomic_replace_leaves_previous_copy(self):
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"new",))
        with patch.object(archive.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first")])
        self.assertFalse(list(self.retained.glob(".usage-*")))


if __name__ == "__main__":
    unittest.main()
