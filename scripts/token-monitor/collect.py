"""Prepare a private scan home and run the official networkless agent."""

import importlib.util
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

spec = importlib.util.spec_from_file_location("antigravity_archive", "/opt/antigravity_archive.py")
antigravity_archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(antigravity_archive)

SNAPSHOT = Path("/snapshot")
HOME = Path("/work/home")
DATA = Path("/state")
PRICING = Path("/opt/tokscale-pricing/cache")


def prepare():
    HOME.mkdir(parents=True)
    cache = DATA / "config/tokscale/cache"
    cache.mkdir(parents=True, exist_ok=True)
    for catalog in PRICING.glob("pricing-*.json"):
        target = cache / catalog.name
        if not target.is_symlink() or target.readlink() != catalog:
            target.unlink(missing_ok=True)
            target.symlink_to(catalog)
    for name in (".claude", ".codex"):
        source = SNAPSHOT / name
        if source.exists():
            (HOME / name).symlink_to(source)
    for relative in (".local/share/opencode", ".gemini/antigravity-cli/conversations"):
        source = SNAPSHOT / relative
        if not source.is_dir():
            continue
        target = HOME / relative
        target.mkdir(parents=True)
        if (source / "storage").is_dir():
            (target / "storage").symlink_to(source / "storage")
        for db in source.glob("*.db"):
            sidecars = [path for suffix in ("-wal", "-journal")
                        if (path := db.with_name(db.name + suffix)).is_file()]
            dest = target / db.name
            if not sidecars and relative == ".local/share/opencode":
                dest.symlink_to(db)
                continue
            shutil.copyfile(db, dest)
            for sidecar in sidecars:
                shutil.copyfile(sidecar, dest.with_name(sidecar.name))
            # Recover only a private copy; SQLite ignores incomplete WAL transactions.
            connection = sqlite3.connect(dest)
            try:
                connection.execute("PRAGMA trusted_schema=OFF")
                connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
                result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if result[0] or connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise RuntimeError("Could not consolidate the SQLite usage snapshot")
            finally:
                connection.close()
            os.utime(dest, ns=(db.stat().st_mtime_ns, db.stat().st_mtime_ns))

    if "antigravity" in os.environ["TOKEN_MONITOR_CLIENTS"].split(","):
        conversations = HOME / ".gemini/antigravity-cli/conversations"
        conversations.mkdir(parents=True, exist_ok=True)
        archive = DATA / "antigravity-usage"
        marker = DATA / "antigravity-usage-required"
        if marker.exists() and not archive.is_dir():
            raise antigravity_archive.ArchiveError("The Antigravity usage archive is missing", 74)
        retained = antigravity_archive.update(conversations, archive, require_index=marker.exists())
        if not marker.exists():
            with marker.open("x") as file:
                file.write("1\n")
                file.flush()
                os.fsync(file.fileno())
            directory = os.open(DATA, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        for path in conversations.glob("*.db"):
            path.unlink()
        for path in retained:
            (conversations / path.name).symlink_to(path)


def main():
    prepare()
    relay = subprocess.Popen([
        "/usr/bin/socat", "TCP4-LISTEN:17321,bind=127.0.0.1,reuseaddr,fork,max-children=2",
        "UNIX-CONNECT:/relay/ingest.sock",
    ])
    try:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(HOME),
            "XDG_CONFIG_HOME": str(DATA / "config"),
            "XDG_CACHE_HOME": str(DATA / "cache"),
            "TOKEN_MONITOR_SHARED_DIR": str(DATA / "archive"),
            "TOKEN_MONITOR_HUB_URL": "http://127.0.0.1:17321",
            "TOKEN_MONITOR_DEVICE_ID": os.environ["TOKEN_MONITOR_DEVICE_ID"],
            "TOKEN_MONITOR_CLIENTS": os.environ["TOKEN_MONITOR_CLIENTS"],
            "TOKEN_MONITOR_LIMITS_ENABLED": "0",
            "TOKEN_MONITOR_OPENCODE_AMBIENT": "0",
            "TOKEN_MONITOR_OPENCODE_LOCAL_LIMITS": "0",
            "TOKEN_MONITOR_SYNC_SESSION_TITLES": "0",
            "TOKEN_MONITOR_SESSION_USAGE_ARCHIVE_ENABLED": "1",
            "TOKEN_MONITOR_PROJECTS_ENABLED": os.environ["TOKEN_MONITOR_PROJECTS_ENABLED"],
            "TOKSCALE_PRICING_CACHE_ONLY": "1",
            "TZ": os.environ["TZ"],
        }
        code = subprocess.run(
            ["/usr/local/bin/node", "--require", "/opt/restore.cjs", "src/agent/agent.js", "--once"], env=env, check=False
        ).returncode
        # Reserve these statuses for the local archive checks, so an upstream
        # exit cannot be mistaken for a host recovery instruction.
        return 1 if code in (72, 73, 74) else code
    finally:
        relay.terminate()
        relay.wait()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except antigravity_archive.ArchiveError as exc:
        raise SystemExit(exc.exit_code)
