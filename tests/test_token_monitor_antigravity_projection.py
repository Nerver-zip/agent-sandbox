from contextlib import closing
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "scripts/token-monitor/antigravity_projection.py"
SPEC = importlib.util.spec_from_file_location("antigravity_projection", MODULE)
projection = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(projection)


def scalar(field, value):
    return projection.enc(field << 3) + projection.enc(value)


def blob(field, value):
    return projection.enc((field << 3) | 2) + projection.enc(len(value)) + value


class ProjectionTest(unittest.TestCase):
    def test_removes_unneeded_nested_content_and_keeps_accounting_fields(self):
        credential = b'{"access_token":"fixture-private-value"}'
        usage = scalar(2, 500) + scalar(5, 16000) + scalar(9, 40) + scalar(10, 300) + blob(11, b"response")
        chat = blob(2, blob(3, credential)) + blob(4, usage) + blob(19, b"gemini-3-flash-a")
        original = blob(1, chat) + scalar(2, 42) + blob(50, b"unrelated")
        projected = projection.project(original, projection.GEN)
        projected_usage = (scalar(2, 500) + scalar(5, 16000) + scalar(9, 40) + scalar(10, 300)
                           + blob(11, projection.response_id(b"response")))
        self.assertEqual(projected, blob(1, blob(4, projected_usage) + blob(19, b"gemini-3-flash-a")) + scalar(2, 42))
        self.assertNotIn(credential, projected)

    def test_projects_timestamp_workspace_and_step_references(self):
        timestamp = scalar(1, 1781502653) + scalar(2, 1)
        trajectory = blob(1, blob(1, b"file:///workspace/project") + blob(7, b"private")) + blob(2, timestamp)
        self.assertEqual(projection.project(trajectory, projection.TRAJECTORY),
                         blob(1, blob(1, b"file:///workspace/project")) + blob(2, timestamp))
        step = blob(1, timestamp) + blob(9, blob(11, b"response") + blob(2, b"private")) + blob(20, scalar(3, 4))
        self.assertEqual(projection.project(step, projection.STEP),
                         blob(1, timestamp) + blob(9, blob(11, projection.response_id(b"response"))) + blob(20, scalar(3, 4)))

    def test_credential_shaped_model_and_workspace_fail_closed(self):
        for data, schema in ((blob(19, b"Bearer fixture-private-value"), projection.CHAT),
                             (blob(1, blob(1, b"file:///home/access_token/project")), projection.TRAJECTORY),
                             (blob(1, blob(1, b"file:///home/access%5ftoken/project")), projection.TRAJECTORY)):
            with self.subTest(schema=schema), self.assertRaisesRegex(ValueError, "credential-shaped"):
                projection.project(data, schema)

    def test_inferred_clock_projects_nested_unknown_fields(self):
        nested = scalar(1, 1781502653) + blob(3, b"fixture-private-value")
        self.assertEqual(projection.inferred_clock(nested), scalar(1, 1781502653))
        raw = b"12345678"
        self.assertEqual(projection.inferred_clock(raw), raw)

    def test_response_ids_are_pseudonymized_consistently(self):
        secret = b"fixture-sensitive-response-id"
        usage = projection.project(blob(11, secret), projection.USAGE)
        step = projection.project(blob(9, blob(11, secret)), projection.STEP)
        self.assertNotIn(secret, usage)
        self.assertNotIn(secret, step)
        self.assertIn(projection.response_id(secret), usage)
        self.assertIn(projection.response_id(secret), step)
        self.assertEqual(projection.project(blob(11, b"  "), projection.USAGE), b"")
        self.assertEqual(projection.project(blob(11, b"\xff"), projection.USAGE), b"")

    def test_database_never_contains_unselected_fixture_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.db"
            target = Path(temp) / "projected.db"
            secret = b"fixture-oauth-private-value"
            with closing(sqlite3.connect(source)) as db, db:
                db.executescript("CREATE TABLE gen_metadata(idx INTEGER,data BLOB); CREATE TABLE steps(idx INTEGER,step_type INTEGER,metadata BLOB,content BLOB); CREATE TABLE tokens(secret BLOB);")
                db.execute("INSERT INTO gen_metadata VALUES (0,?)", (blob(1, blob(2, secret) + blob(4, scalar(2, 15))),))
                db.execute("INSERT INTO steps VALUES (0, 15, ?, ?)", (blob(1, scalar(1, 1781502653)), secret))
                db.execute("INSERT INTO tokens VALUES (?)", (secret,))
            self.assertTrue(projection.project_database(source, target))
            self.assertNotIn(secret, target.read_bytes())
            with closing(sqlite3.connect(target)) as db:
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
                self.assertEqual(names, {"gen_metadata", "steps"})
                self.assertEqual(db.execute("SELECT data FROM gen_metadata").fetchone()[0],
                                 blob(1, blob(4, scalar(2, 15))))

    def test_packed_step_index_discards_unneeded_trailing_bytes(self):
        packed = projection.enc(37) + b"fixture-private-value"
        self.assertEqual(projection.project(blob(2, packed), projection.GEN), blob(2, projection.enc(37)))

    def test_only_monotonic_same_response_generation_updates_are_safe(self):
        identity = projection.response_id(b"response")
        def generation(input_count, output_count, response=identity, model=b"fixture-model"):
            usage = scalar(2, input_count) + scalar(10, output_count) + blob(11, response)
            return blob(1, blob(4, usage) + blob(19, model)) + scalar(2, 3)
        original = generation(10, 2)
        self.assertTrue(projection.monotonic_generation_update(original, generation(15, 3)))
        self.assertFalse(projection.monotonic_generation_update(original, generation(9, 3)))
        self.assertFalse(projection.monotonic_generation_update(original, generation(15, 3, projection.response_id(b"other"))))
        self.assertFalse(projection.monotonic_generation_update(original, generation(15, 3, model=b"other-model")))
        self.assertFalse(projection.monotonic_generation_update(original, generation(15, 3, b"raw-provider-id")))
        # A new explicit response-output field can reduce what Tokscale
        # reports even when every raw counter increases.
        old_fallback = blob(1, blob(4, scalar(3, 100) + blob(11, identity)))
        changed_representation = blob(1, blob(4, scalar(3, 100) + scalar(10, 1) + blob(11, identity)))
        self.assertFalse(projection.monotonic_generation_update(old_fallback, changed_representation))
        self.assertFalse(projection.monotonic_generation_update(original, original + blob(1, b"duplicate")))
        self.assertFalse(projection.monotonic_generation_update(b"not protobuf", generation(15, 3)))

    def test_invalid_wire_data_fails_closed(self):
        for value in (b"\\x00", b"\\x0a\\x80", b"\\x0a\\x02x", b"\\x0b", b"\\x09x", b"\\xff" * 11):
            with self.subTest(value=value), self.assertRaises(ValueError):
                projection.project(value, projection.GEN)


if __name__ == "__main__":
    unittest.main()
