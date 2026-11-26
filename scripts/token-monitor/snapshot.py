"""Copy the supported usage sources without traversing sandbox links or mounts."""

import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
from pathlib import Path

spec = importlib.util.spec_from_file_location("antigravity_projection", Path(__file__).with_name("antigravity_projection.py"))
projection = importlib.util.module_from_spec(spec)
spec.loader.exec_module(projection)
SANITIZER_REVISION = hashlib.sha256(
    b"\0".join(Path(__file__).with_name(name).read_bytes() for name in
               ("antigravity_projection.py", "antigravity_sanitize.py"))
).hexdigest()

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
DB = re.compile(r"opencode(?:-[A-Za-z0-9._-]+)?\.db")
ROOTS = {
    "claude": ((".claude/projects", ".jsonl", 8), (".claude/transcripts", ".jsonl", 8)),
    "codex": ((".codex/sessions", (".jsonl", ".jsonl.zst"), 8),
              (".codex/archived_sessions", (".jsonl", ".jsonl.zst"), 8)),
    "opencode": ((".local/share/opencode/storage/message", ".json", 1),),
    "antigravity": (),
}
OPENCODE = ".local/share/opencode"
ANTIGRAVITY = ".gemini/antigravity-cli/conversations"
SKIP_ERRORS = {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}
MAX_ENTRIES = 500_000
MAX_BYTES = 8 * 1024**3
MAX_FILE = 2 * 1024**3
COPY_CHUNK = 1024 * 1024


class SnapshotError(Exception):
    pass


def mount_id(fd):
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
        for line in info:
            if line.startswith("mnt_id:"):
                return line.split()[1]
    raise SnapshotError("Cannot determine the source mount ID")


def open_source(parent, name, flags, source_mount):
    try:
        fd = os.open(name, flags, dir_fd=parent)
    except OSError as exc:
        if exc.errno in SKIP_ERRORS:
            return None
        raise
    if mount_id(fd) != source_mount:
        os.close(fd)
        return None
    return fd


def open_dir(parent, path, source_mount):
    fd = os.dup(parent)
    for part in path.split("/"):
        try:
            child = open_source(fd, part, DIR_FLAGS, source_mount)
        finally:
            os.close(fd)
        if child is None:
            return None
        fd = child
    return fd


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def file_info(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return None
    return identity(info)


class Snapshot:
    def __init__(self, destination, previous, source_mount, sanitizer=None):
        self.destination = destination
        self.sanitizer = sanitizer
        self.previous = previous
        self.source_mount = source_mount
        self.old_index = (
            json.loads((previous / "index.json").read_text()) if previous else {}
        )
        self.index = {}
        self.entries = 0
        self.bytes = 0
        self.copied = 0

    def names(self, fd):
        with os.scandir(fd) as entries:
            for entry in entries:
                self.entries += 1
                if self.entries > MAX_ENTRIES:
                    raise SnapshotError("Usage source exceeds the snapshot entry limit")
                yield entry

    def open_regular(self, parent, name):
        # Reject devices and sockets before open, then verify the opened descriptor
        # again. The sandbox has no capability to create or mount device nodes.
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None, None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None, None
        fd = open_source(parent, name, FILE_FLAGS, self.source_mount)
        if fd is None:
            return None, None
        info = file_info(fd)
        if info is None:
            os.close(fd)
            return None, None
        return fd, info

    def copy(self, fd, relative, signature, cache_key=None):
        size = signature[2]
        self.bytes += size
        if size > MAX_FILE or self.bytes > MAX_BYTES:
            raise SnapshotError("Usage history exceeds the snapshot byte limit")
        target = self.destination / "home" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        key = signature if cache_key is None else cache_key
        if self.old_index.get(relative) == key:
            os.link(self.previous / "home" / relative, target)
        else:
            with os.fdopen(os.dup(fd), "rb") as src, target.open("xb") as dest:
                remaining = size
                while remaining:
                    chunk = src.read(min(remaining, COPY_CHUNK))
                    if not chunk:
                        raise SnapshotError("A usage source changed during collection")
                    dest.write(chunk)
                    remaining -= len(chunk)
                dest.flush()
                # Tokscale uses file time for usage records without timestamps.
                # Retain the original time across copies and container restore.
                os.utime(dest.fileno(), ns=(signature[3], signature[3]))
                os.fsync(dest.fileno())
            self.copied += size
        if file_info(fd) != signature:
            raise SnapshotError("A usage source changed during collection")
        self.index[relative] = key

    def walk(self, fd, relative, suffix, depth):
        for entry in self.names(fd):
            path = f"{relative}/{entry.name}"
            if depth and entry.is_dir(follow_symlinks=False):
                child = open_source(fd, entry.name, DIR_FLAGS, self.source_mount)
                if child is not None:
                    try:
                        self.walk(child, path, suffix, depth - 1)
                    finally:
                        os.close(child)
            elif entry.name.endswith(suffix):
                source, info = self.open_regular(fd, entry.name)
                if source is not None:
                    try:
                        self.copy(source, path, info)
                    finally:
                        os.close(source)

    def databases(self, fd, relative, pattern):
        for entry in self.names(fd):
            if not pattern.fullmatch(entry.name):
                continue
            if relative == ANTIGRAVITY:
                try:
                    if projection.safe_text(os.fsencode(entry.name)) is None:
                        raise ValueError("invalid session filename")
                except ValueError:
                    raise SnapshotError("Antigravity session filename is not safe to persist") from None
            base, base_info = self.open_regular(fd, entry.name)
            if base is None:
                continue
            companions = []
            try:
                companions.append((entry.name, base, base_info))
                for suffix in ("-wal", "-journal"):
                    name = entry.name + suffix
                    source, info = self.open_regular(fd, name)
                    companions.append((name, source, info))
                group = [info for _, _, info in companions]
                if relative == ANTIGRAVITY:
                    if self.sanitizer is None:
                        raise SnapshotError("Antigravity requires an isolated metadata sanitizer")
                    key = {"format": "agy-projection-v1", "sanitizer": SANITIZER_REVISION, "source": group}
                    path = f"{relative}/{entry.name}"
                    target = self.destination / "home" / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    total = sum(info[2] for _, source, info in companions if source is not None)
                    self.bytes += total
                    if any(info[2] > MAX_FILE for _, source, info in companions if source is not None) or self.bytes > MAX_BYTES:
                        raise SnapshotError("Usage history exceeds the snapshot byte limit")
                    if self.old_index.get(path) == key:
                        os.link(self.previous / "home" / path, target)
                    else:
                        self.sanitizer(companions, target)
                        if target.stat().st_size > MAX_FILE:
                            raise SnapshotError("Projected usage exceeds the snapshot byte limit")
                        os.utime(target, ns=(base_info[3], base_info[3]))
                        with target.open("rb") as output:
                            os.fsync(output.fileno())
                        self.copied += total
                    self.index[path] = key
                else:
                    for name, source, info in companions:
                        if source is not None:
                            self.copy(source, f"{relative}/{name}", info, group)
                for _, source, expected in companions:
                    if source is not None and file_info(source) != expected:
                        raise SnapshotError("Usage database changed during collection")
                # Recheck names for checkpoint, journal creation or replacement.
                for name, source, expected in companions:
                    current, observed = self.open_regular(fd, name)
                    if current is not None:
                        os.close(current)
                    if observed != expected:
                        raise SnapshotError("Usage database changed during collection")
                    if source is None:
                        try:
                            os.stat(name, dir_fd=fd, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise SnapshotError("Database sidecar is not a regular usage file")
            finally:
                for _, source, _ in companions:
                    if source is not None:
                        os.close(source)


def capture(root_fd, destination: Path, previous: Path | None, providers, sanitizer=None):
    """Create a new generation, reusing immutable files from the last capture."""
    destination.mkdir(mode=0o700)
    try:
        source_mount = mount_id(root_fd)
        snapshot = Snapshot(destination, previous, source_mount, sanitizer)
        home = open_dir(root_fd, "home/agent", source_mount)
        if home is None:
            raise SnapshotError("Workspace home is absent or on an unexpected mount")
        try:
            (destination / "home").mkdir()
            for provider in providers:
                for path, suffix, depth in ROOTS[provider]:
                    fd = open_dir(home, path, source_mount)
                    if fd is not None:
                        try:
                            snapshot.walk(fd, path, suffix, depth)
                        finally:
                            os.close(fd)
            if "opencode" in providers:
                fd = open_dir(home, OPENCODE, source_mount)
                if fd is not None:
                    try:
                        snapshot.databases(fd, OPENCODE, DB)
                    finally:
                        os.close(fd)
            if "antigravity" in providers:
                fd = open_dir(home, ANTIGRAVITY, source_mount)
                if fd is not None:
                    try:
                        snapshot.databases(fd, ANTIGRAVITY, re.compile(r"[^/]+\.db"))
                    finally:
                        os.close(fd)
        finally:
            os.close(home)
        with (destination / "index.json").open("x") as index:
            json.dump(snapshot.index, index)
            index.flush()
            os.fsync(index.fileno())
        for directory, _, _ in os.walk(destination, topdown=False):
            fd = os.open(directory, DIR_FLAGS)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return len(snapshot.index), snapshot.copied
    except BaseException:
        shutil.rmtree(destination)
        raise
