"""Host controller for the optional Docker Token Monitor integration."""

import argparse
import fcntl
import filecmp
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from relay import Hub, HubError, relay
from snapshot import ROOTS, SnapshotError, capture

PROJECT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT / ".token-monitor.json"
STATE = PROJECT / ".token-monitor"
DEFAULT_VERSION = "v0.99.0"
DEFAULT_REVISION = "49831523e3bbb3ec57ad6d30001306390592c59b"
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
DEVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
GENERATION = re.compile(r"[0-9]{20}-[0-9a-f]{32}")
UID = 65532
PENDING_LIMIT = 4
RUN_TIMEOUT = 120
SYSTEMD = Path("/etc/systemd/system")
UNIT = "agent-sandbox-token-monitor"


class ConfigurationError(Exception):
    pass


class JobError(Exception):
    pass


def read_private(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ConfigurationError("Configuration and secret files require mode 0600 and must be regular files.")
        if info.st_size > limit:
            raise ConfigurationError("Configuration or secret file exceeds its size limit.")
        data = source.read(limit + 1)
        if len(data) > limit:
            raise ConfigurationError("Configuration or secret file exceeds its size limit.")
        return data.decode("utf-8")


def configuration():
    try:
        data = json.loads(read_private(CONFIG, 16_384))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or type(data.get("enabled", False)) is not bool:
        raise ConfigurationError("Use a JSON object with an optional boolean enabled field.")
    if not data.get("enabled", False):
        return None
    allowed = {
        "enabled", "hub_url", "secret_file", "interval_seconds", "collector_version",
        "device_prefix", "device_id", "providers", "timezone", "projects_enabled",
    }
    if data.keys() - allowed:
        raise ConfigurationError("Unknown setting in .token-monitor.json.")
    url = data.get("hub_url")
    secret_file = data.get("secret_file")
    if not isinstance(url, str) or not isinstance(secret_file, str) or not secret_file:
        raise ConfigurationError("Set hub_url and secret_file in .token-monitor.json.")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise ConfigurationError("Invalid Token Monitor Hub URL.") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.query or parsed.fragment or any(ord(c) < 33 or ord(c) > 126 for c in url)):
        raise ConfigurationError("Use an HTTP(S) Hub URL without credentials, query or fragment.")
    secret_path = PROJECT / secret_file
    if (secret_path.parent.resolve().is_relative_to(PROJECT)
            and secret_path.parent.resolve() / secret_path.name != PROJECT / ".token-monitor.secret"):
        raise ConfigurationError("Use .token-monitor.secret or put the secret file outside the project checkout.")
    secret = read_private(secret_path, 4096).strip()
    if not secret or any(ord(c) < 33 or ord(c) > 126 for c in secret):
        raise ConfigurationError("The Hub secret file must contain a single nonempty printable token.")
    seconds = data.get("interval_seconds", 300)
    if type(seconds) is not int or not 60 <= seconds <= 86400:
        raise ConfigurationError("interval_seconds must be an integer between 60 and 86400.")
    version = data.get("collector_version", DEFAULT_VERSION)
    if not isinstance(version, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ConfigurationError("Set collector_version to an upstream release tag such as v0.99.0.")
    prefix = data.get("device_prefix", "agent-sandbox")
    device = data.get("device_id")
    if (not isinstance(prefix, str) or len(prefix) > 45 or not DEVICE.fullmatch(prefix)
            or (device is not None and (not isinstance(device, str) or not DEVICE.fullmatch(device)))):
        raise ConfigurationError("Use letters, numbers, dots, dashes or underscores in device names.")
    providers = data.get("providers", list(ROOTS))
    if (not isinstance(providers, list) or not providers
            or any(not isinstance(name, str) or name not in ROOTS for name in providers)
            or len(set(providers)) != len(providers)):
        raise ConfigurationError("providers must list distinct values from claude, codex, opencode and antigravity.")
    projects_enabled = data.get("projects_enabled", True)
    if type(projects_enabled) is not bool:
        raise ConfigurationError("projects_enabled must be a boolean.")
    timezone = data.get("timezone", "UTC")
    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ConfigurationError("Set timezone to a system IANA timezone name.") from exc
    return {
        "hub": Hub(url, secret), "interval": seconds, "version": version, "prefix": prefix,
        "device": device, "providers": providers, "timezone": timezone,
        "projects_enabled": projects_enabled,
    }


def durable_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp)
        tmp.write("\n")
        tmp.flush()
        os.fsync(tmp.fileno())
    try:
        os.replace(tmp.name, path)
        sync_dir(path.parent)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def docker(args, timeout=15, *, show=False, discard=False):
    result = subprocess.run(
        DOCKER + args, text=True, stdout=subprocess.DEVNULL if discard else (None if show else subprocess.PIPE),
        stderr=None if show else subprocess.DEVNULL, timeout=timeout, check=False,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root"},
    )
    if result.returncode:
        if args[0] == "run" and "--network=none" in args:
            archive_errors = {
                72: "Antigravity usage archive exceeds its limit.",
                73: "Divergent Antigravity conversation versions require recovery.",
                74: "The Antigravity usage archive is missing; restore it from backup.",
            }
            if result.returncode in archive_errors:
                raise JobError(archive_errors[result.returncode])
        raise JobError(f"Docker {args[0]} failed.")
    return result.stdout


def systemctl(*args):
    result = subprocess.run(["systemctl", *args], timeout=20, check=False)
    if result.returncode:
        raise JobError("Could not configure systemd. Check the Token Monitor unit and systemd journal.")


def systemd_units():
    marker = f"# Managed by Agent Sandbox at {hashlib.sha256(os.fsencode(PROJECT)).hexdigest()}\n"
    path = str(PROJECT / "scripts/token-monitor/manager.py")
    if any(ord(char) < 32 for char in path):
        raise ConfigurationError("The installation path must not contain control characters.")
    path = path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return marker, {
        SYSTEMD / f"{UNIT}.service": f"""[Unit]
Description=Agent Sandbox Token Monitor
After=docker.service network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -E -s "{path}" scheduled
TimeoutStartSec=180
TimeoutStopSec=15
UMask=0077
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=yes
LimitCORE=0
LogRateLimitIntervalSec=1h
LogRateLimitBurst=30
""",
        SYSTEMD / f"{UNIT}.timer": """[Unit]
Description=Schedule Agent Sandbox Token Monitor

[Timer]
OnBootSec=1min
OnUnitInactiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
""",
    }


def manage_units(remove=False):
    marker, definitions = systemd_units()
    for path in definitions:
        if path.is_symlink() or (path.exists() and not path.read_text().startswith(marker)):
            raise ConfigurationError("Token Monitor systemd units already belong to a different installation.")
    if remove:
        if not any(path.exists() for path in definitions):
            return
        systemctl("disable", "--now", f"{UNIT}.timer")
        systemctl("stop", f"{UNIT}.service")
        for path in definitions:
            path.unlink(missing_ok=True)
    else:
        for path, definition in definitions.items():
            body = marker + definition
            if path.exists() and path.read_text() == body:
                continue
            with tempfile.NamedTemporaryFile(mode="w", dir=SYSTEMD, delete=False) as target:
                target.write(body)
                temp = Path(target.name)
            temp.chmod(0o644)
            os.replace(temp, path)
    systemctl("daemon-reload")
    if not remove:
        systemctl("enable", "--now", f"{UNIT}.timer")


def workspace():
    output = docker([
        "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.project=agent-sandbox",
        "--filter", "label=com.docker.compose.service=workspace",
        "--filter", f"label=com.docker.compose.project.working_dir={PROJECT}",
    ])
    ids = output.split()
    if not ids:
        return None
    if len(ids) != 1 or not CONTAINER_ID.fullmatch(ids[0]):
        raise JobError("Expected one Compose workspace for this installation.")
    details = json.loads(docker(["inspect", "--format", "{{json .State}}", ids[0]]))
    return ids[0], int(details["Pid"]), details["StartedAt"]


def open_workspace(container, pid, started_at):
    # A proc directory descriptor refers to this process, even if its numeric
    # PID subsequently gets recycled. Recheck against the Docker daemon.
    proc = os.open(f"/proc/{pid}", os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC)
    try:
        root = os.open("root", os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC, dir_fd=proc)
        if workspace() != (container, pid, started_at):
            os.close(root)
            raise JobError("The Compose workspace changed during collection.")
        return root
    finally:
        os.close(proc)


class Monitor:
    def __init__(self, config):
        self.config = config
        self.hub = config["hub"]
        self.snapshots = STATE / "snapshots"
        self.collector_state = STATE / "collector"
        self.device = self.load_device()
        context = PROJECT / "scripts/token-monitor"
        source_hash = hashlib.sha256()
        for name in ("Dockerfile", "collect.py", "restore.cjs", "antigravity_archive.py"):
            source_hash.update((context / name).read_bytes())
        self.image = f"agent-sandbox-token-monitor:{config['version']}-{source_hash.hexdigest()[:12]}"
        instance = hashlib.sha256(os.fsencode(PROJECT)).hexdigest()[:16]
        self.container_name = f"agent-sandbox-token-monitor-{instance}"

    def require_session_archive(self):
        archive = self.collector_state / "archive"
        for name in ("session-usage-archive.sqlite", "session-usage-archive.json"):
            try:
                info = (archive / name).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_size:
                return
        raise ConfigurationError(
            "The device's session archive is missing. Restore the archive before publishing under this ID."
        )

    def load_device(self):
        path = STATE / "device.json"
        if path.exists():
            device = json.loads(path.read_text())
            if self.config["device"] not in {None, device}:
                raise ConfigurationError("device_id differs from the persisted identity. See migration instructions.")
            status = STATE / "status.json"
            acknowledged = status.exists() and json.loads(status.read_text()).get("last_success")
            if self.config["device"] is not None or acknowledged:
                self.require_session_archive()
            return device
        if self.config["device"] is not None:
            self.require_session_archive()
        device = self.config["device"] or f"{self.config['prefix']}-{uuid.uuid4().hex}"
        durable_json(path, device)
        return device

    def provision(self):
        self.hub.probe()
        try:
            docker(["image", "inspect", self.image])
        except JobError:
            revision = DEFAULT_REVISION if self.config["version"] == DEFAULT_VERSION else ""
            docker([
                "build", "--build-arg", f"COLLECTOR_VERSION={self.config['version']}",
                "--build-arg", f"COLLECTOR_REVISION={revision}", "--tag", self.image,
                str(PROJECT / "scripts/token-monitor"),
            ], timeout=900, show=True)
        self.snapshots.mkdir(exist_ok=True)
        self.collector_state.mkdir(exist_ok=True)
        os.chown(self.collector_state, UID, UID)
        self.save_status()

    def pending(self):
        return sorted(p for p in self.snapshots.iterdir() if GENERATION.fullmatch(p.name))

    def publish(self, root, container):
        existing = self.pending()
        if len(existing) >= PENDING_LIMIT:
            raise JobError("Recovery queue is full. Restore Hub access or use the explicit force path.")
        previous = existing[-1] if existing else self.snapshots / "acknowledged"
        if not previous.is_dir() or (previous / "container").read_text() != container:
            previous = None
        staging = self.snapshots / ".capture"
        if staging.exists():
            shutil.rmtree(staging)
        try:
            files, copied = capture(root, staging, previous, self.config["providers"])
            if existing and previous is not None and copied == 0:
                if filecmp.cmp(staging / "index.json", previous / "index.json", shallow=False):
                    return
            durable_json(staging / "capture.json", {"files": files, "copied_bytes": copied})
            (staging / "container").write_text(container)
            with (staging / "container").open("rb") as marker:
                os.fsync(marker.fileno())
            # The containing host state is 0700. Only the collector bind mount
            # of home/ opens the selected data to the dedicated scanner UID.
            for directory, _, names in os.walk(staging / "home"):
                os.chmod(directory, 0o755)
                for name in names:
                    os.chmod(Path(directory) / name, 0o644)
            stamp = f"{time.time_ns():020d}-{uuid.uuid4().hex}"
            os.rename(staging, self.snapshots / stamp)
            sync_dir(self.snapshots)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def deliver(self, generation):
        with tempfile.TemporaryDirectory(prefix="relay-", dir=STATE) as directory:
            os.chmod(directory, 0o755)
            with relay(Path(directory) / "ingest.sock", self.hub, self.device) as server:
                self._collect(generation, directory)
                if not server.acknowledged:
                    result = f"HTTP {server.last_status}" if server.last_status else "no ingest request"
                    raise JobError(f"Hub did not acknowledge usage ({result}).")
        previous = self.snapshots / "acknowledged"
        if previous.exists():
            shutil.rmtree(previous)
        os.rename(generation, previous)
        sync_dir(self.snapshots)

    def _collect(self, generation, relay_dir):
        try:
            docker([
                "run", "--rm", "--init", "--name", self.container_name, "--pull=never",
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=64", "--memory=2g", "--cpus=1.0", "--user", f"{UID}:{UID}",
                "--log-driver=none", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--tmpfs", f"/work:rw,nosuid,nodev,noexec,size=1024m,uid={UID},gid={UID},mode=0700",
                "--mount", f"type=bind,src={generation / 'home'},dst=/snapshot,readonly",
                "--mount", f"type=bind,src={self.collector_state},dst=/state",
                "--mount", f"type=bind,src={relay_dir},dst=/relay,readonly",
                "--env", f"TOKEN_MONITOR_DEVICE_ID={self.device}",
                "--env", f"TOKEN_MONITOR_CLIENTS={','.join(self.config['providers'])}",
                "--env", f"TOKEN_MONITOR_PROJECTS_ENABLED={int(self.config['projects_enabled'])}",
                "--env", f"TZ={self.config['timezone']}", self.image,
            ], timeout=RUN_TIMEOUT, discard=True)
        except BaseException:
            # Killing the CLI alone does not stop its container.
            try:
                docker(["rm", "-f", self.container_name], timeout=10)
            except (JobError, subprocess.TimeoutExpired):
                pass
            raise

    def save_status(self, **changes):
        path = STATE / "status.json"
        status = json.loads(path.read_text()) if path.exists() else {}
        status.update(changes, device=self.device, pending=len(self.pending()))
        durable_json(path, status)

    def sync(self, *, final=False, scheduled=False):
        path = STATE / "status.json"
        status = json.loads(path.read_text()) if path.exists() else {}
        now = int(time.time())
        if scheduled and status.get("retry_after", 0) > now:
            return
        try:
            source = workspace()
            final_marker = STATE / "last-final.json"
            if final and source:
                if source[1]:
                    self.capture_workspace(*source, stop=True)
                    durable_json(final_marker, [source[0], source[2]])
                    self.final_captured = True
                elif not final_marker.exists() or json.loads(final_marker.read_text()) != [source[0], source[2]]:
                    raise JobError("Stopped workspace has no final capture. Start it with scripts/up.sh and retry.")
                else:
                    self.final_captured = True
            queue = self.pending()
            if not queue and not final and source and source[1]:
                self.capture_workspace(*source)
                queue = self.pending()
            if queue:
                try:
                    self.deliver(queue[0])
                except (JobError, OSError, subprocess.TimeoutExpired):
                    # Capture bounded changes while the Hub is offline.
                    # Leave one slot for the next controlled shutdown.
                    if scheduled and source and source[1] and len(queue) < PENDING_LIMIT - 1:
                        try:
                            self.capture_workspace(*source)
                        except (SnapshotError, JobError, OSError, subprocess.TimeoutExpired):
                            pass  # Preserve the original failure and oldest retry.
                    raise
                self.save_status(last_success=int(time.time()), error=None, failures=0,
                                 retry_after=0 if len(queue) > 1 else now + self.config["interval"])
                if final and len(queue) > 1:
                    print("Warning: older usage synchronized. The final snapshot is queued for automatic recovery.")
            if not source or not source[1] or final:
                cache = self.snapshots / "acknowledged"
                if cache.is_dir():
                    shutil.rmtree(cache)
                    sync_dir(self.snapshots)
        except (SnapshotError, JobError, HubError, OSError, subprocess.TimeoutExpired) as exc:
            message = str(exc) if isinstance(exc, (SnapshotError, JobError, HubError)) else type(exc).__name__
            failures = status.get("failures", 0) + 1
            self.save_status(error=message, failures=failures,
                             retry_after=now + min(3600, self.config["interval"] * 2 ** min(failures - 1, 6)))
            if final and self.pending() and getattr(self, "final_captured", False):
                print(f"Warning: {message} The final snapshot is queued for automatic recovery.")
                return
            raise JobError(message) from exc

    def capture_workspace(self, container, pid, started_at, *, stop=False):
        if len(self.pending()) >= PENDING_LIMIT:
            raise JobError("Recovery queue is full. Restore Hub access or use the explicit force path.")
        root = open_workspace(container, pid, started_at)
        try:
            if stop:
                # Invalidate an earlier stop receipt before stopping again.
                (STATE / "last-final.json").unlink(missing_ok=True)
                sync_dir(STATE)
                # Keep a root descriptor open while the workspace shuts down
                # so even its last flushed writes are available before removal.
                docker(["stop", "--time", "30", container], timeout=40)
            self.publish(root, container)
        finally:
            os.close(root)


def private_state():
    STATE.mkdir(mode=0o700, exist_ok=True)
    info = STATE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ConfigurationError("Token Monitor state must be a private root-owned directory.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["setup", "sync", "scheduled", "final", "status", "start", "uninstall"])
    action = parser.parse_args().command
    try:
        if action == "uninstall":
            if os.geteuid() != 0:
                raise ConfigurationError("Run uninstall with sudo on the Docker host.")
            os.umask(0o077)
            manage_units(remove=True)
            if CONFIG.exists() or CONFIG.is_symlink():
                private_state()
                with (STATE / "lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    saved = STATE / f"disabled-config-{time.time_ns()}.json"
                    CONFIG.rename(saved)
                    sync_dir(STATE)
                    sync_dir(PROJECT)
            print("Token Monitor disabled. Local configuration, archive and Hub history retained.")
            return 0
        config = configuration()
        if config is None:
            if action in {"setup", "status", "sync"}:
                print("Token Monitor integration is disabled.")
            return 0
        if os.geteuid() != 0:
            raise ConfigurationError("Run the Token Monitor controller with sudo on the Docker host.")
        if action == "start":
            if not (STATE / "device.json").is_file():
                raise ConfigurationError("Run scripts/token-monitor.sh setup before starting the collector.")
            systemctl("start", "--no-block", f"{UNIT}.service")
            return 0
        os.umask(0o077)
        private_state()
        with (STATE / "lock").open("a") as lock:
            if action == "status":
                path = STATE / "status.json"
                if path.exists():
                    status = json.loads(path.read_text())
                    for field in ("last_success", "retry_after"):
                        if status.get(field):
                            status[field] = datetime.fromtimestamp(status[field], timezone.utc).isoformat()
                    print(json.dumps(status, indent=2), flush=True)
                else:
                    print("Token Monitor has no synchronization history.", flush=True)
                subprocess.run(["systemctl", "show", f"{UNIT}.timer", f"{UNIT}.service",
                                "-p", "ActiveState", "-p", "SubState", "-p", "NextElapseUSecMonotonic"],
                               timeout=10, check=False)
                try:
                    config["hub"].probe()
                    print("Hub connectivity OK.")
                except HubError as exc:
                    print(exc)
                return 0
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if action == "scheduled" else 0))
            monitor = Monitor(config)
            if action == "setup":
                monitor.provision()
                manage_units()
                monitor.sync()
            else:
                monitor.sync(final=action == "final", scheduled=action == "scheduled")
    except BlockingIOError:
        if action != "scheduled":
            print("Token Monitor collector is busy.")
            return 1
    except (ConfigurationError, JobError, HubError, SnapshotError) as exc:
        print(f"Token Monitor: {exc}", file=os.sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Token Monitor: {type(exc).__name__}. Check local configuration, Docker and state.", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Allow the Docker cleanup path to run if a service manager stops the job.
    signal.signal(signal.SIGTERM, lambda *_: os.sys.exit(143))
    raise SystemExit(main())
