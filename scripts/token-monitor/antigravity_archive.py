"""Retain the SQLite columns needed by the pinned Antigravity scanner."""

from contextlib import closing
import importlib.util
import hashlib
import filecmp
import json
import os
import re
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


def merge_disjoint(older, candidate):
    """Retain disjoint identified rows only when their original dates survive.

    The candidate is a disposable file. Reject any ambiguity instead of
    inventing a relationship between two generations or timestamp clocks.
    """
    if not metadata_preserved(older, candidate):
        return False
    with closing(sqlite3.connect(candidate)) as db, db:
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("ATTACH DATABASE ? AS retained", (str(older),))
        tables = {row[0] for row in db.execute("SELECT name FROM retained.sqlite_schema WHERE type='table'")}
        if "trajectory_metadata_blob" not in tables:
            return False
        anchor = db.execute("SELECT data FROM retained.trajectory_metadata_blob LIMIT 1").fetchone()
        if not anchor or not projection.session_has_created_at(anchor[0]):
            return False
        identities = {}
        for schema in ("retained", "main"):
            if db.execute(f"SELECT 1 FROM {schema}.gen_metadata GROUP BY idx HAVING idx IS NULL OR COUNT(*) != 1 LIMIT 1").fetchone():
                return False
            observed = set()
            for idx, data in db.execute(f"SELECT idx,data FROM {schema}.gen_metadata"):
                identity = projection.generation_identity(data)
                if identity is None or identity in observed:
                    return False
                observed.add(identity)
                if schema == "retained":
                    identities[identity] = idx
                elif identity in identities and identities[identity] != idx:
                    # The same response at a new index can select a different
                    # step timestamp even when its usage bytes are identical.
                    return False
        if db.execute("""
            SELECT 1 FROM retained.gen_metadata old JOIN main.gen_metadata current ON old.idx=current.idx
            WHERE old.data IS NOT current.data LIMIT 1
        """).fetchone():
            return False
        db.execute("""
            INSERT INTO main.gen_metadata(idx,data)
            SELECT old.idx,old.data FROM retained.gen_metadata old
            WHERE NOT EXISTS (SELECT 1 FROM main.gen_metadata current WHERE current.idx=old.idx)
        """)
        return True


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def state_path(path):
    # Do not embed an untrusted session filename in a metadata path.
    key = hashlib.sha256(os.fsencode(path.name)).hexdigest()
    return path.parent / f".checksum-{key}.json"


def durable_json(path, value):
    fd, temp_name = tempfile.mkstemp(prefix=".atomic-", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def verify_checksum(path, *, legacy=False):
    state = state_path(path)
    try:
        digest = checksum(path)
        mtime = path.stat().st_mtime_ns
    except OSError:
        raise ArchiveError("The Antigravity usage archive is unreadable; restore it from backup", 74) from None
    if not state.exists() and legacy:
        durable_json(state, {"name": path.name, "hashes": [digest], "mtimes": [mtime]})
        return digest
    try:
        value = json.loads(state.read_text())
        hashes = value["hashes"]
        mtimes = value.get("mtimes")
        if (value.get("name") != path.name or not isinstance(hashes, list)
                or not 1 <= len(hashes) <= 2
                or any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in hashes)
                or digest not in hashes
                or (mtimes is not None and (not isinstance(mtimes, list) or len(mtimes) != len(hashes)
                    or any(type(item) is not int for item in mtimes)
                    or (digest, mtime) not in zip(hashes, mtimes)))):
            raise ValueError("archive checksum mismatch")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ArchiveError("The Antigravity usage archive is incomplete or corrupt; restore it from backup", 74) from None
    if len(hashes) != 1 or mtimes is None:
        # A previous interruption occurred on either side of an atomic replace.
        # Old checksum records did not protect mtime; their current value is
        # the only available migration baseline.
        durable_json(state, {"name": path.name, "hashes": [digest], "mtimes": [mtime]})
    return digest


def update(source_dir, archive_dir, *, require_index=False, conflicts=None):
    """Atomically retain compatible observations; return retained DB paths.

    A divergent version cannot be merged safely using opaque upstream records.
    Stop the collection rather than silently dropping or duplicating usage.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    # The host lock serializes collectors. An interrupted previous job may
    # have left a private, uncommitted candidate; never scan it as a session.
    for pattern in (".usage-*", ".index-*", ".atomic-*"):
        for stale in archive_dir.glob(pattern):
            stale.unlink()
    retained = list(archive_dir.glob("*.db"))
    index = archive_dir / ".index.json"
    legacy = not index.exists()
    expected = []
    if index.exists():
        try:
            document = json.loads(index.read_text())
            legacy = isinstance(document, list)
            expected = document if legacy else document["files"]
            if not legacy and document.get("version") != 2:
                raise ValueError("invalid archive version")
            if (not isinstance(expected, list) or len(expected) > MAX_DATABASES
                    or any(not isinstance(name, str) or Path(name).name != name
                           or not name.endswith(".db") for name in expected)
                    or len(expected) != len(set(expected))):
                raise ValueError("invalid archive index")
            if not set(expected).issubset({path.name for path in retained}):
                raise ValueError("missing archive database")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ArchiveError("The Antigravity usage archive is incomplete; restore it from backup", 74) from None
    elif require_index:
        raise ArchiveError("The Antigravity usage archive index is missing; restore it from backup", 74)
    if len(retained) > MAX_DATABASES or sum(p.stat().st_size for p in retained) > MAX_BYTES:
        raise ArchiveError("Antigravity usage archive exceeds its limit", 72)
    used = sum(p.stat().st_size for p in retained)
    hashes = {path.name: verify_checksum(path, legacy=legacy) for path in retained}
    # Establish checksums before upgrading the manifest. An interrupted upgrade
    # can safely retry the old format; the reverse order could accept lost data.
    if legacy:
        durable_json(index, {"version": 2, "files": sorted(path.name for path in retained)})
        expected = sorted(path.name for path in retained)
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
                        if not merge_disjoint(dest, temp):
                            raise ArchiveError("Divergent Antigravity conversation versions require recovery", 73)
                        # SQLite changed the candidate's mtime. Restore the
                        # source time; the verified session anchor dates rows.
                        os.utime(temp, ns=(source.stat().st_mtime_ns, source.stat().st_mtime_ns))
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
            new_hash = checksum(temp)
            previous_hash = hashes.get(dest.name)
            new_mtime = temp.stat().st_mtime_ns
            previous = (previous_hash, dest.stat().st_mtime_ns) if previous_hash else None
            allowed = list(dict.fromkeys(item for item in (previous, (new_hash, new_mtime)) if item))
            # Write-ahead permits either complete version after a crash.
            durable_json(state_path(dest), {"name": dest.name, "hashes": [item[0] for item in allowed],
                                            "mtimes": [item[1] for item in allowed]})
            os.replace(temp, dest)
            directory = os.open(archive_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            durable_json(state_path(dest), {"name": dest.name, "hashes": [new_hash], "mtimes": [new_mtime]})
            hashes[dest.name] = new_hash
            used += size - old_size
            if not old_size:
                retained.append(dest)
        except ArchiveError as exc:
            if exc.exit_code != 73 or conflicts is None:
                raise
            # Do not expose an untrusted filename in diagnostics.
            conflicts.append(hashlib.sha256(os.fsencode(source.name)).hexdigest())
        finally:
            temp.unlink(missing_ok=True)
    retained = sorted(archive_dir.glob("*.db"))
    names = [path.name for path in retained]
    if expected != names:
        durable_json(index, {"version": 2, "files": names})
    return retained
