"""Static guardrails. Do not confuse them with runtime escape/egress testing."""
from pathlib import Path
import subprocess
import tomllib
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ProjectContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = yaml.safe_load((ROOT / "compose.yaml").read_text())
        cls.s = cls.c["services"]["workspace"]

    def test_hardening(self):
        self.assertFalse(self.s["privileged"])
        self.assertNotEqual(self.s.get("network_mode"), "host")
        self.assertIn("ALL", self.s["cap_drop"])
        self.assertIn("AUDIT_WRITE", self.s["cap_add"])
        self.assertIn("no-new-privileges:true", self.s["security_opt"])
        self.assertLessEqual(self.s["pids_limit"], 256)
        self.assertNotIn("/var/run/docker.sock", str(self.s))

    def test_exact_mount_allowlist(self):
        self.assertEqual(
            set(self.s["volumes"]),
            {
                "./persistent/ssh/ssh_host_ed25519_key:/etc/ssh/ssh_host_ed25519_key:ro",
                "./persistent/ssh/ssh_host_ed25519_key.pub:/etc/ssh/ssh_host_ed25519_key.pub:ro",
                "./persistent/ssh/authorized_keys:/home/agent/.ssh/authorized_keys:rw",
                "./persistent/ssh/known_hosts:/home/agent/.ssh/known_hosts:rw",
                "./persistent/codex/auth.json:/home/agent/.codex/auth.json:rw",
                "./persistent/gh/token:/opt/agent-secrets/gh_token:ro",
                "./persistent/git:/home/agent/.config/git:rw",
            },
        )
        self.assertEqual(len(self.s["volumes"]), 7)

    def test_network(self):
        self.assertTrue(self.c["networks"]["sandbox"]["external"])
        self.assertTrue(self.s["ports"][0].startswith("${TAILSCALE_IP:"))
        self.assertNotIn("GH_TOKEN", self.s["environment"])
        self.assertFalse(any("100.118.53.107" in str(x) for x in self.s["environment"].values()))
        self.assertEqual(self.s["environment"]["DOCKER_HOST"], "tcp://dind:2375")
        self.assertIn("dind", self.s["depends_on"])

    def test_dind_service_and_isolation(self):
        self.assertIn("dind", self.c["services"])
        dind = self.c["services"]["dind"]
        self.assertTrue(dind["privileged"])
        self.assertEqual(dind.get("networks"), ["sandbox"])
        self.assertNotIn("/var/run/docker.sock", str(dind))
        self.assertNotIn("ports", dind)
        self.assertEqual(dind.get("restart"), "no")
        self.assertIn("docker:27-dind", dind.get("image", ""))

    def test_image_contains_expected_tools(self):
        d = (ROOT / "Dockerfile").read_text()
        for v in (
            "debian:trixie-slim", "gh gnupg", "starship", "fastfetch", "eza",
            "docker-cli docker-compose docker-buildx socat", "DOCKER_HOST=tcp://dind:2375",
            "zsh-autosuggestions", "zsh-syntax-highlighting", "openssh-server",
            "npm install --global", "@openai/codex", "antigravity.google/cli/install.sh",
            "config/tmux.conf",
            "gh-wrapper.sh", "credential.https://github.com.helper", "useradd --create-home",
            ".config/git",
        ):
            with self.subTest(v=v):
                self.assertIn(v, d)
        sshd = (ROOT / "sshd_config").read_text()
        for v in ("PasswordAuthentication no", "AllowUsers agent", "PermitRootLogin no", "AllowAgentForwarding no"):
            self.assertIn(v, sshd)

    def test_shell_defaults(self):
        z = (ROOT / "config/agent.zshrc").read_text()
        for v in ("oh-my-zsh.sh", "starship init zsh", "fastfetch", "eza --icons", "zsh-autosuggestions", "DOCKER_HOST"):
            self.assertIn(v, z)
        tomllib.loads((ROOT / "config/starship.toml").read_text())
        tomllib.loads((ROOT / "config/codex.toml").read_text())
        tmux = (ROOT / "config/tmux.conf").read_text()
        self.assertIn("mouse on", tmux)
        self.assertIn("history-limit 100000", tmux)

    def test_setup_persists_only_explicit_files(self):
        setup = (ROOT / "scripts/setup.sh").read_text()
        for v in (
            "ssh-keygen -y", "ssh/host_keys/", "/etc/ssh/ssh_host_ed25519_key",
            "persistent/ssh/authorized_keys", "persistent/ssh/known_hosts",
            "persistent/codex/auth.json", "persistent/gh/token", "persistent/git",
            "chown 10001:10001", "chmod 0400",
        ):
            self.assertIn(v, setup)
        gh = (ROOT / "scripts/gh-wrapper.sh").read_text()
        self.assertIn("export GH_TOKEN", gh)
        self.assertIn("exec /usr/bin/gh", gh)
        self.assertIn("/opt/agent-secrets/gh_token", gh)
        self.assertIn("read -rs TOKEN", (ROOT / "scripts/set-gh-token.sh").read_text())
        self.assertIn("set-gh-token.sh", (ROOT / "README.md").read_text())

    def test_destroy_removes_all_generated_state(self):
        d = (ROOT / "scripts/destroy.sh").read_text()
        for v in (
            "docker compose down --remove-orphans --volumes",
            "firewall.sh remove", "persistent/ssh/ssh_host_ed25519_key",
            "persistent/ssh/authorized_keys", "persistent/ssh/known_hosts",
            "persistent/codex/auth.json", "persistent/gh/token", "persistent/git/config",
            "ssh/authorized_keys .env",
        ):
            self.assertIn(v, d)

    def test_all_shell_scripts_have_valid_bash_syntax(self):
        for file in (ROOT / "scripts").glob("*.sh"):
            with self.subTest(file=file.name):
                subprocess.run(["bash", "-n", str(file)], check=True)

    def test_everything_is_english(self):
        for file in (ROOT / "scripts").glob("*.sh"):
            self.assertNotIn("Destruir", file.read_text())


    def test_dockerignore_allows_all_copied_scripts(self):
        di = (ROOT / '.dockerignore').read_text()
        self.assertIn('!scripts/container-entrypoint.sh', di)
        self.assertIn('!scripts/gh-wrapper.sh', di)

    def test_entrypoint_docker_sock_proxy(self):
        entrypoint = (ROOT / "scripts/container-entrypoint.sh").read_text()
        self.assertIn("socat UNIX-LISTEN:/var/run/docker.sock", entrypoint)
        self.assertIn("TCP:dind:2375", entrypoint)

if __name__ == "__main__":
    unittest.main()
