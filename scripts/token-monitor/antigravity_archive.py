"""Retain the SQLite columns needed by the pinned Antigravity scanner."""

from contextlib import closing
import importlib.util
import filecmp
import json
import os
import sqlite3
import tempfile
from pathlib import Path

TABLES = {
    "gen_metadata": ("idx", "data"),
    "trajectory_metadata_blob": ("data",),
    "steps": ("idx", "step_type", "metadata"),
}
MAX_BYTES = 8 * 1024**3
MAX_DATABASES = 500_000

spec = importlib.util.spec_from_file_location("antigravity_projection", Path(__file__).with_name("antigravity_projection.py"))
projection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(projection)


class ArchiveError(RuntimeError):
    def __init__(self, message, exit_code):
        super().__init__(message)
        self.exit_code = exit_code


def compact(source, target):
    """Copy accounting columns only, from an already recovered private DB."""
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst, dst:
        src.execute("PRAGMA trusted_schema=OFF")
        present = {row[0] for row in src.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        if "gen_metadata" not in present:
            return False
        for table, fields in TABLES.items():
            if table not in present:
                continue
            columns = ", ".join(fields)
            dst.execute(f"CREATE TABLE {table} ({columns})")
            suffix = {"gen_metadata": " ORDER BY idx", "steps": " WHERE metadata IS NOT NULL",
                      "trajectory_metadata_blob": " LIMIT 1"}[table]
            dst.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' for _ in fields)})",
                            src.execute(f"SELECT {columns} FROM {table}{suffix}"))
    return True


def generations_subset(older, newer):
    """Compare multisets, so duplicate rows cannot silently disappear."""
    with closing(sqlite3.connect(f"{older.as_uri()}?mode=ro", uri=True)) as db:
        db.execute("ATTACH DATABASE ? AS newer", (str(newer),))
        query = """
            WITH old AS (SELECT idx, data, COUNT(*) AS n FROM main.gen_metadata GROUP BY idx, data),
                 current AS (SELECT idx, data, COUNT(*) AS n FROM newer.gen_metadata GROUP BY idx, data)
            SELECT 1 FROM old LEFT JOIN current ON current.idx IS old.idx AND current.data IS old.data
            WHERE old.n > COALESCE(current.n, 0) LIMIT 1
        """
        return db.execute(query).fetchone() is None


def metadata_preserved(older, newer):
    """Do not replace retained workspace or step clocks with an incomplete copy."""
    with closing(sqlite3.connect(f"{older.as_uri()}?mode=ro", uri=True)) as db:
        db.execute("ATTACH DATABASE ? AS newer", (str(newer),))
        old_tables = {row[0] for row in db.execute("SELECT name FROM main.sqlite_schema WHERE type='table'")}
        new_tables = {row[0] for row in db.execute("SELECT name FROM newer.sqlite_schema WHERE type='table'")}
        for table, columns in (("trajectory_metadata_blob", ("data",)),
                               ("steps", ("idx", "step_type", "metadata"))):
            if table not in old_tables:
                continue
            if table not in new_tables:
                if db.execute(f"SELECT 1 FROM main.{table} LIMIT 1").fetchone():
                    return False
                continue
            fields = ", ".join(columns)
            equality = " AND ".join(f"current.{field} IS old.{field}" for field in columns)
            query = f"""
                WITH old AS (SELECT {fields}, COUNT(*) AS n FROM main.{table} GROUP BY {fields}),
                     current AS (SELECT {fields}, COUNT(*) AS n FROM newer.{table} GROUP BY {fields})
                SELECT 1 FROM old LEFT JOIN current ON {equality}
                WHERE old.n > COALESCE(current.n, 0) LIMIT 1
            """
            if db.execute(query).fetchone():
                return False
        return True


def generations_advance(older, newer):
    """Recognize monotonic rewrites of the same identified generation only."""
    with closing(sqlite3.connect(f"{older.as_uri()}?mode=ro", uri=True)) as db:
        db.execute("ATTACH DATABASE ? AS newer", (str(newer),))
        for schema in ("main", "newer"):
            if db.execute(f"SELECT 1 FROM {schema}.gen_metadata GROUP BY idx HAVING idx IS NULL OR COUNT(*) != 1 LIMIT 1").fetchone():
                return False
        rows = db.execute("""
            SELECT old.data, current.data FROM main.gen_metadata old
            LEFT JOIN newer.gen_metadata current ON current.idx = old.idx
        """)
        for old, current in rows:
            if current is None or (old != current and not projection.monotonic_generation_update(old, current)):
                return False
        return True


def update(source_dir, archive_dir, *, require_index=False):
    """Atomically retain compatible observations; return retained DB paths.

    A divergent version cannot be merged safely using opaque upstream records.
    Stop the collection rather than silently dropping or duplicating usage.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    # The host lock serializes collectors. An interrupted previous job may
    # have left a private, uncommitted candidate; never scan it as a session.
    for pattern in (".usage-*", ".index-*"):
        for stale in archive_dir.glob(pattern):
            stale.unlink()
    retained = list(archive_dir.glob("*.db"))
    index = archive_dir / ".index.json"
    if index.exists():
        try:
            expected = json.loads(index.read_text())
            if (not isinstance(expected, list) or len(expected) > MAX_DATABASES
                    or any(not isinstance(name, str) or Path(name).name != name
                           or not name.endswith(".db") for name in expected)
                    or len(expected) != len(set(expected))):
                raise ValueError("invalid archive index")
            if not set(expected).issubset({path.name for path in retained}):
                raise ValueError("missing archive database")
        except (OSError, ValueError, TypeError):
            raise ArchiveError("The Antigravity usage archive is incomplete; restore it from backup", 74) from None
    elif require_index:
        raise ArchiveError("The Antigravity usage archive index is missing; restore it from backup", 74)
    if len(retained) > MAX_DATABASES or sum(p.stat().st_size for p in retained) > MAX_BYTES:
        raise ArchiveError("Antigravity usage archive exceeds its limit", 72)
    used = sum(p.stat().st_size for p in retained)
    for source in sorted(source_dir.glob("*.db")):
        dest = archive_dir / source.name
        fd, temp_name = tempfile.mkstemp(prefix=".usage-", dir=archive_dir)
        os.close(fd)
        temp = Path(temp_name)
        try:
            if not compact(source, temp):
                continue
            os.utime(temp, ns=(source.stat().st_mtime_ns, source.stat().st_mtime_ns))
            if dest.exists():
                if (temp.stat().st_mtime_ns == dest.stat().st_mtime_ns
                        and filecmp.cmp(temp, dest, shallow=False)):
                    continue
                old_in_new = generations_subset(dest, temp)
                new_in_old = generations_subset(temp, dest)
                if new_in_old and (not old_in_new or temp.stat().st_mtime_ns <= dest.stat().st_mtime_ns):
                    continue  # Older or unchanged observation.
                if not old_in_new:
                    if generations_advance(temp, dest):
                        continue  # A prior version of identified generations.
                    if not generations_advance(dest, temp):
                        raise ArchiveError("Divergent Antigravity conversation versions require recovery", 73)
                if not metadata_preserved(dest, temp):
                    if new_in_old:
                        continue
                    raise ArchiveError("Divergent Antigravity conversation metadata requires recovery", 73)
            elif len(retained) >= MAX_DATABASES:
                raise ArchiveError("Antigravity usage archive exceeds its limit", 72)
            size = temp.stat().st_size
            old_size = dest.stat().st_size if dest.exists() else 0
            if used - old_size + size > MAX_BYTES:
                raise ArchiveError("Antigravity usage archive exceeds its limit", 72)
            with temp.open("rb") as file:
                os.fsync(file.fileno())
            os.replace(temp, dest)
            directory = os.open(archive_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            used += size - old_size
            if not old_size:
                retained.append(dest)
        finally:
            temp.unlink(missing_ok=True)
    retained = sorted(archive_dir.glob("*.db"))
    names = [path.name for path in retained]
    if not index.exists() or expected != names:
        fd, temp_name = tempfile.mkstemp(prefix=".index-", dir=archive_dir)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(names, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, index)
            directory = os.open(archive_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temp.unlink(missing_ok=True)
    return retained
