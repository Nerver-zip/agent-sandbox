"""Project an isolated SQLite database and WAL supplied on stdin.

The caller must run this process inside a networkless, resource-limited
container with /work and /tmp mounted as private tmpfs. No input paths or
archives from the sandbox are extracted by name.
"""

import importlib.util
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("antigravity_projection", Path(__file__).with_name("antigravity_projection.py"))
projection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(projection)

MAX_INPUT = 2 * 1024**3


def main():
    os.umask(0o077)
    work = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("/work")
    work.mkdir(parents=True, exist_ok=True)
    names = {"usage.db", "usage.db-wal", "usage.db-journal"}
    seen = set()
    total = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as stream:
        for member in stream:
            if member.name not in names or member.name in seen or not member.isfile():
                raise ValueError("invalid input member")
            seen.add(member.name)
            total += member.size
            if member.size < 0 or total > MAX_INPUT:
                raise ValueError("input too large")
            source = stream.extractfile(member)
            if source is None:
                raise ValueError("unreadable input")
            with source, (work / member.name).open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    if "usage.db" not in seen:
        raise ValueError("missing database")
    db = work / "usage.db"
    connection = sqlite3.connect(db)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0] or connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise ValueError("database recovery failed")
    finally:
        connection.close()
    output = work / "projected.db"
    if not projection.project_database(db, output):
        with sqlite3.connect(output) as connection:
            connection.execute("CREATE TABLE gen_metadata(idx INTEGER, data BLOB)")
    with output.open("rb") as data:
        shutil.copyfileobj(data, sys.stdout.buffer, 1024 * 1024)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, tarfile.TarError, TypeError, ValueError):
        # Do not include provider data or exception text in container logs.
        raise SystemExit(1)
