from contextlib import closing
import importlib.util
import os
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

    def test_identified_monotonic_rewrite_and_new_row_advance_archive(self):
        p = archive.projection
        def scalar(field, value):
            return p.enc(field << 3) + p.enc(value)
        def blob(field, value):
            return p.enc((field << 3) | 2) + p.enc(len(value)) + value
        def generation(identity, input_count):
            usage = scalar(2, input_count) + scalar(10, 2) + blob(11, p.response_id(identity))
            return blob(1, blob(4, usage) + blob(19, b"fixture-model"))
        original = generation(b"first", 10)
        advanced = generation(b"first", 20)
        new_row = generation(b"second", 5)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (original,))
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=? WHERE idx=0", (advanced,))
            db.execute("INSERT INTO gen_metadata VALUES (1,?)", (new_row,))
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, advanced), (1, new_row)])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=? WHERE idx=0", (generation(b"first", 5),))
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, advanced), (1, new_row)])
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=? WHERE idx=0", (generation(b"other", 30),))
        with self.assertRaisesRegex(archive.ArchiveError, "Divergent"):
            archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, advanced), (1, new_row)])

    def test_new_generations_do_not_erase_retained_workspace_or_step_metadata(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (b"workspace",))
            db.execute("INSERT INTO steps(idx,step_type,metadata) VALUES (1,15,?)", (b"clock",))
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO gen_metadata VALUES (1,?)", (b"second",))
            db.execute("DELETE FROM steps")
        with self.assertRaisesRegex(archive.ArchiveError, "metadata"):
            archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first")])

    def test_conflicting_conversation_preserves_old_and_accepts_independent_session(self):
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (b"incompatible",))
        other = self.source / "other.db"
        with closing(sqlite3.connect(other)) as db, db:
            db.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB)")
            db.execute("INSERT INTO gen_metadata VALUES(0, ?)", (b"independent",))
        conflicts = []
        archive.update(self.source, self.retained, conflicts=conflicts)
        self.assertEqual(self.read_rows(), [(0, b"first")])
        self.assertEqual(len(conflicts), 1)
        self.assertNotIn("session", conflicts[0])
        with closing(sqlite3.connect(self.retained / other.name)) as db:
            self.assertEqual(db.execute("SELECT data FROM gen_metadata").fetchall(), [(b"independent",)])

    def test_disjoint_identified_generations_merge_with_stable_anchor(self):
        p = archive.projection
        def scalar(field, value):
            return p.enc(field << 3) + p.enc(value)
        def blob(field, value):
            return p.enc((field << 3) | 2) + p.enc(len(value)) + value
        def generation(identity, amount):
            return blob(1, blob(4, scalar(2, amount) + blob(11, p.response_id(identity))) + blob(19, b"fixture-model"))
        first, retained, fresh = generation(b"a", 10), generation(b"b", 20), generation(b"c", 30)
        anchor = blob(1, blob(1, b"file:///workspace/project")) + blob(2, scalar(1, 1781502653))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (first,))
            db.execute("INSERT INTO gen_metadata VALUES (1,?)", (retained,))
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (anchor,))
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM gen_metadata WHERE idx=1")
            db.execute("INSERT INTO gen_metadata VALUES (2,?)", (fresh,))
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, first), (1, retained), (2, fresh)])
        archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, first), (1, retained), (2, fresh)])

    def test_disjoint_merge_rejects_conflicting_identity_at_same_index(self):
        p = archive.projection
        def scalar(field, value):
            return p.enc(field << 3) + p.enc(value)
        def blob(field, value):
            return p.enc((field << 3) | 2) + p.enc(len(value)) + value
        def generation(identity):
            return blob(1, blob(4, scalar(2, 10) + blob(11, p.response_id(identity))))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (generation(b"first"),))
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (blob(2, scalar(1, 1781502653)),))
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (generation(b"different"),))
            db.execute("INSERT INTO gen_metadata VALUES (1,?)", (generation(b"new"),))
        with self.assertRaisesRegex(archive.ArchiveError, "Divergent"):
            archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, generation(b"first"))])

    def test_disjoint_merge_rejects_response_moved_to_a_different_index(self):
        p = archive.projection
        def scalar(field, value):
            return p.enc(field << 3) + p.enc(value)
        def blob(field, value):
            return p.enc((field << 3) | 2) + p.enc(len(value)) + value
        def generation(identity):
            return blob(1, blob(4, scalar(2, 10) + blob(11, p.response_id(identity))))
        original, other = generation(b"same"), generation(b"other")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (original,))
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (blob(2, scalar(1, 1781502653)),))
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM gen_metadata")
            db.execute("INSERT INTO gen_metadata VALUES (1,?)", (original,))
            db.execute("INSERT INTO gen_metadata VALUES (2,?)", (other,))
        with self.assertRaisesRegex(archive.ArchiveError, "Divergent"):
            archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, original)])

    def test_disjoint_merge_rejects_missing_identity_or_session_anchor(self):
        p = archive.projection
        def scalar(field, value):
            return p.enc(field << 3) + p.enc(value)
        def blob(field, value):
            return p.enc((field << 3) | 2) + p.enc(len(value)) + value
        row = blob(1, blob(4, scalar(2, 10) + blob(11, p.response_id(b"a"))))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE gen_metadata SET data=?", (row,))
            db.execute("INSERT INTO gen_metadata VALUES (1,?)", (row,))
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM gen_metadata WHERE idx=1")
            db.execute("INSERT INTO gen_metadata VALUES (2,?)", (row,))
        with self.assertRaisesRegex(archive.ArchiveError, "Divergent"):
            archive.update(self.source, self.retained)
        # A usable anchor still cannot justify merging an unidentified row.
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM gen_metadata WHERE idx=2")
            db.execute("INSERT INTO trajectory_metadata_blob VALUES (?)", (blob(2, scalar(1, 1781502653)),))
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DELETE FROM gen_metadata WHERE idx=1")
            db.execute("INSERT INTO gen_metadata VALUES (2,?)", (blob(1, blob(4, scalar(2, 20))),))
        with self.assertRaisesRegex(archive.ArchiveError, "Divergent"):
            archive.update(self.source, self.retained)

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

    def test_missing_database_and_index_stop_collection(self):
        archive.update(self.source, self.retained)
        original = self.retained / self.db.name
        backup = original.read_bytes()
        original.unlink()
        with self.assertRaises(archive.ArchiveError) as error:
            archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(error.exception.exit_code, 74)
        self.assertFalse(original.exists())
        original.write_bytes(backup)
        (self.retained / ".index.json").unlink()
        with self.assertRaises(archive.ArchiveError) as error:
            archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(error.exception.exit_code, 74)

    def test_extra_database_after_interrupted_index_update_is_retained(self):
        archive.update(self.source, self.retained)
        second = self.retained / "new-session.db"
        second.write_bytes((self.retained / self.db.name).read_bytes())
        archive.durable_json(archive.state_path(second), {"name": second.name, "hashes": [archive.checksum(second)]})
        self.db.unlink()
        self.assertEqual([p.name for p in archive.update(self.source, self.retained, require_index=True)],
                         ["new-session.db", "session.db"])
        second.unlink()
        with self.assertRaises(archive.ArchiveError):
            archive.update(self.source, self.retained, require_index=True)

    def test_corrupt_retained_database_and_missing_checksum_stop_collection(self):
        archive.update(self.source, self.retained)
        retained = self.retained / self.db.name
        original = retained.read_bytes()
        retained.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        self.db.unlink()
        with self.assertRaises(archive.ArchiveError) as error:
            archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(error.exception.exit_code, 74)
        retained.write_bytes(original)
        archive.state_path(retained).unlink()
        with self.assertRaises(archive.ArchiveError) as error:
            archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(error.exception.exit_code, 74)

    def test_changed_retained_mtime_stops_collection(self):
        archive.update(self.source, self.retained)
        retained = self.retained / self.db.name
        original = retained.stat().st_mtime_ns
        os.utime(retained, ns=(original + 10_000_000_000, original + 10_000_000_000))
        self.db.unlink()
        with self.assertRaises(archive.ArchiveError) as error:
            archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(error.exception.exit_code, 74)
        os.utime(retained, ns=(original, original))
        archive.update(self.source, self.retained, require_index=True)

    def test_interruption_before_database_replace_recovers_valid_old_version(self):
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"new",))
        replace = archive.os.replace
        def interrupt_database(source, target):
            if Path(target) == self.retained / self.db.name:
                raise OSError("interrupted before database replace")
            return replace(source, target)
        with patch.object(archive.os, "replace", side_effect=interrupt_database):
            with self.assertRaises(OSError):
                archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first")])
        archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(self.read_rows(), [(0, b"first"), (1, b"new")])

    def test_interrupted_mtime_only_update_retains_both_valid_versions(self):
        archive.update(self.source, self.retained)
        retained = self.retained / self.db.name
        old_mtime = retained.stat().st_mtime_ns
        new_mtime = old_mtime + 10_000_000_000
        os.utime(self.db, ns=(new_mtime, new_mtime))
        replace = archive.os.replace
        def interrupt_database(source, target):
            if Path(target) == retained:
                raise OSError("interrupted before database replace")
            return replace(source, target)
        with patch.object(archive.os, "replace", side_effect=interrupt_database):
            with self.assertRaises(OSError):
                archive.update(self.source, self.retained)
        state = __import__("json").loads(archive.state_path(retained).read_text())
        self.assertEqual(state["hashes"][0], state["hashes"][1])
        self.assertEqual(state["mtimes"], [old_mtime, new_mtime])
        archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(retained.stat().st_mtime_ns, new_mtime)

    def test_interruption_after_database_replace_recovers_valid_new_version(self):
        archive.update(self.source, self.retained)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("INSERT INTO gen_metadata VALUES (1, ?)", (b"new",))
        original = archive.durable_json
        def interrupt_final_state(path, value):
            if path == archive.state_path(self.retained / self.db.name) and len(value["hashes"]) == 1:
                raise OSError("interrupted after replace")
            return original(path, value)
        with patch.object(archive, "durable_json", side_effect=interrupt_final_state):
            with self.assertRaises(OSError):
                archive.update(self.source, self.retained)
        self.assertEqual(self.read_rows(), [(0, b"first"), (1, b"new")])
        archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(self.read_rows(), [(0, b"first"), (1, b"new")])
        state = __import__("json").loads(archive.state_path(self.retained / self.db.name).read_text())
        self.assertEqual(len(state["hashes"]), 1)

    def test_legacy_index_migrates_without_losing_archive(self):
        archive.update(self.source, self.retained)
        retained = self.retained / self.db.name
        archive.state_path(retained).unlink()
        (self.retained / ".index.json").write_text('["session.db"]')
        self.db.unlink()
        archive.update(self.source, self.retained, require_index=True)
        self.assertEqual(self.read_rows(), [(0, b"first")])
        self.assertTrue(archive.state_path(retained).is_file())
        self.assertEqual(__import__("json").loads((self.retained / ".index.json").read_text())["version"], 2)

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
