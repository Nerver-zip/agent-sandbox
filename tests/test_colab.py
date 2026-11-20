"""Behavioral tests for the Google Colab backend, using fake colab/ssh commands."""
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]

FAKE_COLAB = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state = Path(os.environ["FAKE_COLAB_STATE"])
state.mkdir(parents=True, exist_ok=True)
sessions_file = state / "sessions.json"
calls_file = state / "calls.log"


def load():
    try:
        return json.loads(sessions_file.read_text())
    except Exception:
        return {}


def save(data):
    sessions_file.write_text(json.dumps(data, indent=2) + "\n")


args = sys.argv[1:]
while args and args[0].startswith("--"):
    if args[0] in ("--auth", "--config") and len(args) > 1:
        args = args[2:]
    else:
        args = args[1:]
command = args[0] if args else ""
rest = args[1:]
with calls_file.open("a") as handle:
    handle.write(json.dumps([command] + rest) + "\n")

if command == "version":
    print("Version: 0.8.0")
    sys.exit(0)
if command == "ssh" and "--help" in rest:
    print("--proxy-mode")
    sys.exit(0)
if command == "usage":
    if os.environ.get("FAKE_COLAB_FAIL_USAGE") == "1":
        print("[colab] authentication failed", file=sys.stderr)
        sys.exit(1)
    print("Current balance: 100.00 compute units")
    print("Usage rate: 0.00/hr")
    print("Active assignments: %d" % len(load()))
    sys.exit(0)
if command == "sessions":
    if os.environ.get("FAKE_COLAB_FAIL_SESSIONS") == "1":
        print("[colab] backend lookup failed", file=sys.stderr)
        sys.exit(1)
    sessions = load()
    orphan = os.environ.get("FAKE_ORPHAN_ENDPOINT", "")
    if orphan:
        print("[?] %s | Hardware: CPU | Shape: Standard | Variant: DEFAULT" % orphan)
    if not sessions and not orphan:
        print("[colab] No active sessions found on server.")
        sys.exit(0)
    for name, data in sessions.items():
        print("[%s] %s | Hardware: %s | Shape: %s | Variant: %s | Status: IDLE" % (
            name, data["endpoint"], data["hardware"], data["shape"], data["variant"]))
    sys.exit(0)
if command == "status":
    name = None
    if "-s" in rest:
        name = rest[rest.index("-s") + 1]
    sessions = load()
    if name in sessions:
        data = sessions[name]
        print("[%s] %s | Hardware: %s | Shape: %s | Variant: %s | Status: IDLE" % (
            name, data["endpoint"], data["hardware"], data["shape"], data["variant"]))
    else:
        print("[colab] Session '%s' not found." % name)
    sys.exit(0)
if command == "new":
    if os.environ.get("FAKE_COLAB_FAIL_NEW") == "1":
        print("[colab] allocation refused", file=sys.stderr)
        sys.exit(1)
    name = None
    gpu = None
    tpu = None
    high_mem = False
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in ("-s", "--session"):
            name = rest[index + 1]
            index += 2
        elif item == "--gpu":
            gpu = rest[index + 1].upper()
            index += 2
        elif item == "--tpu":
            tpu = rest[index + 1].lower()
            index += 2
        elif item == "--high-mem":
            high_mem = True
            index += 1
        else:
            index += 1
    sessions = load()
    if name in sessions:
        print("VIOLATION: duplicate new for %s" % name, file=sys.stderr)
        sys.exit(1)
    if gpu:
        hardware, variant = gpu, "GPU"
    elif tpu:
        hardware, variant = tpu.upper(), "TPU"
    else:
        hardware, variant = "CPU", "DEFAULT"
    sessions[name] = {
        "endpoint": "ep-%s-%d" % (name, len(sessions) + 1),
        "hardware": hardware,
        "variant": variant,
        "shape": "High-RAM" if high_mem else "Standard",
    }
    save(sessions)
    print("[colab] Creating session '%s'..." % name)
    print("[colab] Session READY.")
    sys.exit(0)
if command == "stop":
    if os.environ.get("FAKE_COLAB_FAIL_STOP") == "1":
        print("[colab] stop failed", file=sys.stderr)
        sys.exit(1)
    name = None
    if "-s" in rest:
        name = rest[rest.index("-s") + 1]
    sessions = load()
    sessions.pop(name, None)
    save(sessions)
    print("[colab] Stopping session '%s'..." % name)
    print("[colab] Session terminated.")
    sys.exit(0)
print("[colab] unknown command: %s" % command, file=sys.stderr)
sys.exit(2)
'''

FAKE_SSH = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state = Path(os.environ["FAKE_SSH_STATE"])
state.mkdir(parents=True, exist_ok=True)
remote_root = Path(os.environ.get("FAKE_REMOTE_ROOT", str(state / "remote")))
remote_root.mkdir(parents=True, exist_ok=True)
marker = state / "bootstrap-version"
calls = state / "calls.log"

args = sys.argv[1:]
host = None
remote = []
index = 0
while index < len(args):
    item = args[index]
    if item in ("-F", "-o", "-i", "-p", "-l"):
        index += 2
    elif item.startswith("-"):
        index += 1
    else:
        host = item
        remote = args[index + 1:]
        break
command = " ".join(remote)
with calls.open("a") as handle:
    handle.write(json.dumps({"host": host, "command": command}) + "\n")


def read_stdin():
    try:
        return sys.stdin.buffer.read()
    except Exception:
        return b""


if command == "bash -s":
    read_stdin()
    print("Host: fake-colab")
    print("Kernel: Linux fake")
    print("Uptime: up 1 hour")
    print("CPUs: 2")
    print("Memory: 1Gi used / 12Gi total")
    print("Disk: 1Gi used / 226Gi total (1%)")
    print("Workspace: 1M")
    print("Bootstrap: " + (marker.read_text().strip() if marker.exists() else "missing"))
    for name in ("zsh", "tmux", "nvim", "starship", "eza", "fastfetch", "node",
                 "python3", "git", "cmake", "gh", "codex", "claude", "opencode", "agy"):
        print("%s: fake" % name)
    sys.exit(0)

if "bootstrap.sh" in command:
    version = None
    parts = command.split()
    if "--version" in parts:
        version = parts[parts.index("--version") + 1]
    attempts_file = state / "bootstrap-attempts"
    try:
        attempts = int(attempts_file.read_text())
    except Exception:
        attempts = 0
    attempts += 1
    attempts_file.write_text(str(attempts))
    if os.environ.get("FAKE_BOOTSTRAP_FAIL_ONCE") == "1" and attempts == 1:
        (state / "bootstrap.log").write_text("[bootstrap] simulated failure\n")
        print("[bootstrap] simulated failure", file=sys.stderr)
        sys.exit(1)
    if version:
        marker.write_text(version + "\n")
    print("[bootstrap] complete")
    sys.exit(0)

if "tar -xzf -" in command and "/tmp/agent-sandbox-bootstrap" in command:
    (state / "uploaded.tar.gz").write_bytes(read_stdin())
    sys.exit(0)

if "tar -xzf -" in command:
    (state / "restore.tar.gz").write_bytes(read_stdin())
    sys.exit(0)

if "tar -C" in command and "-czf -" in command:
    os.execvp("tar", ["tar", "-czf", "-", "-C", str(remote_root), "workspace"])

if "cat /root/.agent-sandbox/bootstrap-version" in command:
    if marker.exists():
        print(marker.read_text().strip())
    sys.exit(0)

if "tail -n 80" in command and "bootstrap.log" in command:
    print("[bootstrap] simulated failure")
    sys.exit(0)

if "gh auth login --with-token" in command:
    token_file = state / "gh-token"
    token_file.write_text(read_stdin().decode("utf-8").strip() + "\n")
    token_file.chmod(0o600)
    print("Logged in")
    sys.exit(0)

if "gh auth setup-git" in command:
    sys.exit(0)

if "command -v gh" in command:
    sys.exit(0)

if "printf ready" in command:
    print("ready")
    sys.exit(0)

sys.exit(0)
'''


class ColabScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        shutil.copytree(ROOT / "scripts" / "colab", self.root / "scripts" / "colab")
        shutil.copy2(ROOT / "scripts" / "agent-notify.sh", self.root / "scripts" / "agent-notify.sh")
        shutil.copytree(ROOT / "config", self.root / "config")
        (self.root / "home").mkdir()
        (self.root / "bin").mkdir()
        (self.root / "tmp").mkdir()
        self.colab_state = self.root / "fake-colab"
        self.ssh_state = self.root / "fake-ssh"
        self.remote_root = self.root / "remote"
        self.remote_root.mkdir()
        self.fake_colab = self.root / "bin" / "colab"
        self.fake_ssh = self.root / "bin" / "ssh"
        self.fake_colab.write_text(FAKE_COLAB)
        self.fake_ssh.write_text(FAKE_SSH)
        self.fake_colab.chmod(0o755)
        self.fake_ssh.chmod(0o755)
        (self.root / "home" / ".gitconfig").write_text(
            "[user]\n\tname = Test User\n\temail = test@example.com\n"
        )
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.root / "home"),
            "PATH": str(self.root / "bin") + ":" + os.environ.get("PATH", ""),
            "TMPDIR": str(self.root / "tmp"),
            "GIT_CONFIG_GLOBAL": str(self.root / "home" / ".gitconfig"),
            "COLAB_CLI": str(self.fake_colab),
            "COLAB_SKIP_AUTH_CHECK": "1",
            "FAKE_COLAB_STATE": str(self.colab_state),
            "FAKE_SSH_STATE": str(self.ssh_state),
            "FAKE_REMOTE_ROOT": str(self.remote_root),
            "NO_COLOR": "1",
            "TERM": "dumb",
        })
        self.setup()

    def tearDown(self):
        self.temp.cleanup()

    def run_script(self, name, *args, check=True, input_text=None, env=None, timeout=60):
        merged = self.env.copy()
        if env:
            merged.update(env)
        result = subprocess.run(
            ["bash", str(self.root / "scripts" / "colab" / name), *args],
            cwd=self.root,
            env=merged,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            self.fail(f"{name} failed ({result.returncode})\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
        return result

    def setup(self):
        return self.run_script("setup.sh")

    def colab_calls(self):
        path = self.colab_state / "calls.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def ssh_calls(self):
        path = self.ssh_state / "calls.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def clear_calls(self):
        for path in (self.colab_state / "calls.log", self.ssh_state / "calls.log"):
            if path.exists():
                path.unlink()

    def sessions(self):
        path = self.colab_state / "sessions.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def write_sessions(self, data):
        (self.colab_state / "sessions.json").write_text(json.dumps(data, indent=2) + "\n")

    def config_text(self):
        return (self.root / ".colab" / "config.env").read_text()

    def set_config(self, key, value):
        path = self.root / ".colab" / "config.env"
        lines = path.read_text().splitlines()
        replacement = f"{key}={value}"
        found = False
        output = []
        for line in lines:
            if line.startswith(key + "="):
                output.append(replacement)
                found = True
            else:
                output.append(line)
        if not found:
            output.append(replacement)
        path.write_text("\n".join(output) + "\n")

    def bootstrap_marker(self):
        path = self.ssh_state / "bootstrap-version"
        return path.read_text().strip() if path.exists() else None

    def test_setup_creates_private_state_and_dedicated_key(self):
        colab_dir = self.root / ".colab"
        config = colab_dir / "config.env"
        key = colab_dir / "ssh" / "id_ed25519"
        self.assertEqual(stat.S_IMODE(colab_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertTrue((key.parent / "id_ed25519.pub").is_file())
        gitconfig_text = (colab_dir / "gitconfig").read_text()
        self.assertIn("name = Test User", gitconfig_text)
        self.assertIn("email = test@example.com", gitconfig_text)
        text = self.config_text()
        self.assertIn("COLAB_SESSION=agent-sandbox", text)
        self.assertIn(f"COLAB_CLI={self.fake_colab}", text)
        self.assertIn("COLAB_SSH_KEY=.colab/ssh/id_ed25519", text)
        ssh_config = (colab_dir / "ssh" / "config").read_text()
        self.assertIn("Host agent-sandbox-colab", ssh_config)
        self.assertIn("Host agent-sandbox-colab-force", ssh_config)
        self.assertIn("--proxy-mode", ssh_config)
        self.assertIn("agent-sandbox-colab-force", ssh_config)
        self.assertIn("--force", ssh_config)
        self.assertNotIn("id_rsa", self.config_text())

    def test_up_provisions_one_cpu_runtime_and_bootstraps(self):
        result = self.run_script("up.sh")
        self.assertIn("Agent Sandbox on Google Colab is ready", result.stdout)
        sessions = self.sessions()
        self.assertEqual(list(sessions), ["agent-sandbox"])
        self.assertEqual(sessions["agent-sandbox"]["hardware"], "CPU")
        news = [call for call in self.colab_calls() if call and call[0] == "new"]
        self.assertEqual(len(news), 1)
        self.assertIn("-s", news[0])
        self.assertIn("agent-sandbox", news[0])
        self.assertNotIn("--gpu", news[0])
        bootstraps = [call for call in self.ssh_calls() if "bootstrap.sh" in call["command"]]
        self.assertEqual(len(bootstraps), 1)
        self.assertIsNotNone(self.bootstrap_marker())
        self.assertIn("ep-agent-sandbox-1", self.config_text())

    def test_up_reuses_runtime_and_skips_current_bootstrap(self):
        self.run_script("up.sh")
        self.clear_calls()
        result = self.run_script("up.sh")
        self.assertIn("skipping reinstallation", result.stdout)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])
        self.assertEqual([call for call in self.ssh_calls() if "bootstrap.sh" in call["command"]], [])

    def test_up_rebuild_forces_bootstrap_without_new_runtime(self):
        self.run_script("up.sh")
        self.clear_calls()
        self.run_script("up.sh", "--rebuild")
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])
        bootstraps = [call for call in self.ssh_calls() if "bootstrap.sh" in call["command"]]
        self.assertEqual(len(bootstraps), 1)
        self.assertIn("--force", bootstraps[0]["command"])

    def test_up_refuses_to_create_when_session_lookup_fails(self):
        result = self.run_script("up.sh", check=False, env={"FAKE_COLAB_FAIL_SESSIONS": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])
        self.assertIn("No runtime was created", result.stderr)

    def test_up_refuses_to_create_when_auth_check_fails(self):
        result = self.run_script("up.sh", check=False, env={"FAKE_COLAB_FAIL_USAGE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])

    def test_up_does_not_retry_a_failed_allocation(self):
        result = self.run_script("up.sh", check=False, env={"FAKE_COLAB_FAIL_NEW": "1"})
        self.assertNotEqual(result.returncode, 0)
        news = [call for call in self.colab_calls() if call and call[0] == "new"]
        self.assertEqual(len(news), 1)
        self.assertIn("No retry was attempted", result.stderr)

    def test_interrupted_bootstrap_is_resumed_without_a_second_runtime(self):
        first = self.run_script("up.sh", check=False, env={"FAKE_BOOTSTRAP_FAIL_ONCE": "1"})
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(len([call for call in self.colab_calls() if call and call[0] == "new"]), 1)
        self.assertIsNone(self.bootstrap_marker())
        second = self.run_script("up.sh")
        self.assertIn("Agent Sandbox on Google Colab is ready", second.stdout)
        self.assertEqual(len([call for call in self.colab_calls() if call and call[0] == "new"]), 1)
        self.assertIsNotNone(self.bootstrap_marker())

    def test_orphaned_endpoint_blocks_provisioning(self):
        self.set_config("COLAB_ENDPOINT", "ep-lost-runtime")
        result = self.run_script("up.sh", check=False, env={"FAKE_ORPHAN_ENDPOINT": "ep-lost-runtime"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])
        self.assertIn("local session entry is gone", result.stderr)

    def test_hardware_change_requires_explicit_release(self):
        self.run_script("up.sh")
        result = self.run_script("up.sh", "--gpu", "T4", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([call for call in self.colab_calls() if call and call[0] == "new"]), 1)
        self.assertIn("down.sh first", result.stderr)

    def test_session_switch_refuses_an_active_runtime(self):
        self.run_script("up.sh")
        result = self.run_script("up.sh", "--session", "other", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len([call for call in self.colab_calls() if call and call[0] == "new"]), 1)
        self.assertIn("still active", result.stderr)

    def test_connect_requires_runtime_and_force_selects_force_host(self):
        result = self.run_script("connect.sh", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])
        self.run_script("up.sh")
        self.clear_calls()
        self.run_script("connect.sh")
        calls = self.ssh_calls()
        self.assertEqual(calls[-1]["host"], "agent-sandbox-colab")
        self.run_script("connect.sh", "--force")
        self.assertEqual(self.ssh_calls()[-1]["host"], "agent-sandbox-colab-force")
        self.run_script("connect.sh", "--", "echo", "hello")
        self.assertEqual(self.ssh_calls()[-1]["command"], "echo hello")

    def test_proxy_refuses_to_auto_create(self):
        self.clear_calls()
        result = self.run_script("proxy.sh", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("will not create a runtime", result.stderr)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "ssh"], [])

    def test_status_never_provisions(self):
        self.clear_calls()
        result = self.run_script("status.sh")
        self.assertIn("No active Colab runtime", result.stdout)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])
        self.run_script("up.sh")
        self.clear_calls()
        result = self.run_script("status.sh")
        self.assertIn("Bootstrap state: current", result.stdout)
        self.assertEqual([call for call in self.colab_calls() if call and call[0] == "new"], [])

    def test_down_stops_only_the_tracked_session(self):
        self.run_script("up.sh")
        sessions = self.sessions()
        sessions["someone-else"] = {
            "endpoint": "ep-other-9",
            "hardware": "CPU",
            "variant": "DEFAULT",
            "shape": "Standard",
        }
        self.write_sessions(sessions)
        result = self.run_script("down.sh", "--yes")
        self.assertIn("terminated", result.stdout)
        remaining = self.sessions()
        self.assertEqual(list(remaining), ["someone-else"])
        self.assertIn("COLAB_ENDPOINT=''", self.config_text())
        stops = [call for call in self.colab_calls() if call and call[0] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertIn("agent-sandbox", stops[0])
        self.assertNotIn("--all", stops[0])

    def test_down_without_runtime_is_clean(self):
        result = self.run_script("down.sh", "--yes")
        self.assertIn("No active Colab runtime", result.stdout)
        self.assertIn("COLAB_ENDPOINT=''", self.config_text())

    def test_save_creates_private_valid_workspace_archive(self):
        self.run_script("up.sh")
        workspace = self.remote_root / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("hello from colab\n")
        destination = self.root / "backups" / "test.tar.gz"
        result = self.run_script("save.sh", str(destination))
        self.assertIn("Saved", result.stdout)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        with tarfile.open(destination, "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("workspace/hello.txt", names)

    def test_restore_uses_a_new_directory_unless_forced(self):
        self.run_script("up.sh")
        archive = self.root / "restore-source.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            data = b"restored\n"
            info = tarfile.TarInfo("workspace/hello.txt")
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
        self.clear_calls()
        self.run_script("restore.sh", str(archive))
        calls = self.ssh_calls()
        self.assertIn("--transform", calls[-1]["command"])
        self.assertIn("workspace.restore-", calls[-1]["command"])
        self.run_script("restore.sh", "--force", str(archive))
        self.assertEqual(self.ssh_calls()[-1]["command"], "tar -xzf - -C /home/agent")

    def test_set_gh_token_uses_stdin_and_never_argv(self):
        self.run_script("up.sh")
        token = "github_pat_TESTTOKEN123"
        result = self.run_script("set-gh-token.sh", input_text=token + "\n")
        self.assertIn("authenticated", result.stdout)
        token_file = self.ssh_state / "gh-token"
        self.assertEqual(token_file.read_text().strip(), token)
        for call in self.colab_calls() + self.ssh_calls():
            self.assertNotIn(token, json.dumps(call))
        invalid = self.run_script("set-gh-token.sh", input_text="not-a-token\n", check=False)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertEqual(token_file.read_text().strip(), token)

    def test_flag_validation_happens_before_any_api_call(self):
        self.clear_calls()
        result = self.run_script("up.sh", "--gpu", "NOPE", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.colab_calls(), [])
        result = self.run_script("up.sh", "--session", "bad name", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.colab_calls(), [])


class ColabStaticContractTests(unittest.TestCase):
    def test_all_colab_scripts_have_valid_bash_syntax(self):
        files = sorted((ROOT / "scripts" / "colab").glob("*.sh"))
        self.assertGreaterEqual(len(files), 10)
        for file in files:
            with self.subTest(file=file.name):
                subprocess.run(["bash", "-n", str(file)], check=True)

    def test_bootstrap_contract(self):
        text = (ROOT / "scripts" / "colab" / "bootstrap.sh").read_text()
        for value in (
            "set -Eeuo pipefail",
            "bootstrap-version",
            "apt-get",
            "npm install --global",
            "config/agent.zshrc",
            "sha256sum -c",
            "--force",
            "chmod 0700 \"$STATE_DIR\"",
            "usermod -s",
        ):
            self.assertIn(value, text)
        for forbidden in ("keep-alive", "keepalive", "GH_TOKEN", "persistent/", "colab new", "colab stop --all"):
            self.assertNotIn(forbidden, text)

    def test_bootstrap_installer_regressions(self):
        text = (ROOT / "scripts" / "colab" / "bootstrap.sh").read_text()
        # The live smoke test found that npm's default global prefix pointed
        # inside the versioned Node directory and that the checksum looked for
        # the wrong filename.
        for value in (
            "npm config set prefix /usr/local",
            "NPM_CONFIG_PREFIX=/usr/local",
            '"/tmp/$node_tarball"',
            'NVIM_LOG="$STATE_DIR/nvim-bootstrap.log"',
        ):
            self.assertIn(value, text)
        setup = (ROOT / "scripts" / "colab" / "setup.sh").read_text()
        self.assertIn("SSH_HELP=$(", setup)
        self.assertNotIn("ssh --help 2>&1 | grep -q", setup)

    def test_colab_scripts_never_copy_local_credentials(self):
        for file in (ROOT / "scripts" / "colab").glob("*.sh"):
            text = file.read_text()
            for forbidden in ("persistent/gh", "persistent/codex", "persistent/claude", "id_rsa", "GH_TOKEN="):
                with self.subTest(file=file.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, text)

    def test_gitignore_and_dockerignore_cover_colab_state(self):
        self.assertIn(".colab/", (ROOT / ".gitignore").read_text())
        self.assertIn("backups/", (ROOT / ".gitignore").read_text())
        self.assertIn(".colab", (ROOT / ".dockerignore").read_text())
        self.assertIn("backups", (ROOT / ".dockerignore").read_text())

    def test_shared_shell_config_is_portable(self):
        zshrc = (ROOT / "config" / "agent.zshrc").read_text()
        self.assertIn("DOCKER_HOST", zshrc)
        self.assertNotIn("DOCKER_HOST:-tcp://dind", zshrc)
        self.assertIn('cd "$HOME/workspace"', zshrc)
        codex = (ROOT / "config" / "codex.toml").read_text()
        self.assertIn("CODEX_HOME", codex)
        self.assertNotIn("entrypoint symlink", codex)

    def test_documentation_has_colab_quick_start(self):
        readme = (ROOT / "README.md").read_text()
        for value in (
            "Google Colab",
            "./scripts/colab/setup.sh",
            "./scripts/colab/up.sh",
            "./scripts/colab/connect.sh",
            "./scripts/colab/status.sh",
            "./scripts/colab/down.sh",
            "./scripts/colab/save.sh",
        ):
            self.assertIn(value, readme)
        colab_doc = (ROOT / "docs" / "COLAB.md").read_text()
        for value in ("Architecture", "Feature parity", "Security", "Troubleshooting", "Manual verification"):
            self.assertIn(value, colab_doc)


if __name__ == "__main__":
    unittest.main()
