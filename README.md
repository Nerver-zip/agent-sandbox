# Agent Sandbox

Agent Sandbox gives coding agents a disposable development environment, separated from your everyday host workspace. Use it to work on repositories, run development tools, and discard temporary files after a task. Choose a Docker workspace on your own Linux host or a Google Colab runtime accessed over SSH.

The environment includes [Codex CLI](https://github.com/openai/codex) (`codex`), [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`), [Antigravity CLI](https://antigravity.google/docs/cli/install/) (`agy`), and [OpenCode](https://opencode.ai/) (`opencode`), plus Git, GitHub CLI, Neovim, tmux, Python, Node.js, and C/C++ tools. Colab installs Antigravity on a best-effort basis.

## Choose a backend

| | Docker | Google Colab |
| --- | --- | --- |
| Runs on | Your Linux host | A disposable Google-managed VM |
| Connects through | SSH to the host's Tailscale address | Colab CLI's WebSocket SSH proxy |
| User | Unprivileged `agent` | `root` |
| Docker workloads | Isolated Docker-in-Docker sidecar | Not supported |
| Workspace | Preserved on stop; cleared on container recreation | Lost when the runtime is released or expires |
| Agent sign-in | Codex and Claude credentials persist; OpenCode and Antigravity do not | All agent credentials disappear with the runtime |
| Token Monitor | Optional host integration | Not integrated |

Both backends require internet access to install tools and contact agent services. Colab availability, accelerator access, and compute charges depend on your Google account and plan. See the [Docker guide](docs/DOCKER.md) and [Colab guide](docs/COLAB.md) for details.

## Docker quickstart

On a Linux host, you need rootful Docker Engine with Compose v2, Tailscale connected on `tailscale0`, an iptables-based Docker firewall with the `DOCKER-USER` chain, `iproute2`, `sudo`, OpenSSH tools, Python 3, and Git. Rootless Docker, Docker Desktop, remote Docker contexts, and Docker's native nftables backend are not supported. The host needs access to the image and package registries during setup.

```bash
git clone https://github.com/Nerver-zip/agent-sandbox.git
cd agent-sandbox

# Pass a public key accessible on the host, never a private key.
./scripts/setup.sh ~/.ssh/id_ed25519.pub
./scripts/up.sh
./scripts/verify.sh
```

From a client authorized by that key and your tailnet policy, connect to the host's Tailscale address:

```bash
ssh -p 2222 agent@your-tailscale-host
```

The port defaults to `2222` and can be configured in `.env`. Verify the server fingerprint against `ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub` on the host. Setup can also import a Git identity; authentication and additional options are in the [Docker guide](docs/DOCKER.md).

## Google Colab quickstart

On a Linux or macOS client, use a Google account with Colab access, OpenSSH, Git, `tar`, `curl`, and Python 3. If the Colab CLI is not already installed, setup can install it with `uv` or `pipx`.

```bash
git clone https://github.com/Nerver-zip/agent-sandbox.git
cd agent-sandbox
./scripts/colab/setup.sh
./scripts/colab/up.sh
./scripts/colab/connect.sh
./scripts/colab/status.sh
```

Setup may prompt for Google authentication. The default runtime uses CPU; accelerator options, cache settings, and recovery are described in the [Colab guide](docs/COLAB.md).

## Work and persistence

Inside either environment, start the agent you want and authenticate it interactively. Push changes to Git or back them up before removing a workspace. The four agents are independent; their login state is not shared. For Docker, `scripts/set-gh-token.sh`, `scripts/import-git-config.sh`, and `scripts/reset-git-config.sh` manage the optional GitHub token and Git identity; see the Docker guide before using them.

| Docker command | Effect |
| --- | --- |
| `./scripts/stop.sh` / `./scripts/up.sh` | Stop and restart existing containers; preserve the workspace. |
| `./scripts/stop.sh --down` | Remove containers and their workspace; keep configured host credentials and SSH identity. |
| `./scripts/up.sh --rebuild` | Rebuild and recreate containers; discard the workspace while keeping configured host credentials and SSH identity. |
| `./scripts/destroy.sh` | Remove the sandbox, workspace, generated SSH identity, and persistent credentials after confirmation. |

Docker persists Codex and Claude login files, the configured GitHub token, Git identity, and SSH host/client state under `persistent/`. OpenCode and Antigravity logins remain in the disposable container. None of these agent logins persist on Colab after release. Git pushes and `./scripts/colab/save.sh` provide separate persistence for Colab work; `./scripts/colab/down.sh` releases the runtime. Saved workspace archives can contain secrets.

The Docker lifecycle scripts attempt a final Token Monitor capture when it is enabled. Do not use raw Compose removal if you need that capture. See [Docker lifecycle and credentials](docs/DOCKER.md#lifecycle-and-persistence) and [Colab persistence](docs/COLAB.md#persistence-matrix).

## Token Monitor

The optional [Token Monitor integration](docs/TOKEN_MONITOR.md) reports usage from all four agents to a separate Hub. It runs on the Docker host, is disabled by default, and keeps its own archive across ordinary sandbox resets. The guide covers setup, existing installations with explicit provider lists, historical retention, recovery, and upstream accounting limitations. Colab has no Token Monitor lifecycle integration.

## Security

Docker containers share the host kernel; this project is not a VM or a secure boundary against a kernel escape. The workspace has public internet access, and code running as the agent can read and transmit credentials available to that account. The Docker backend uses host firewall rules to restrict new connections to the host, LAN, tailnet, and other Docker networks, and exposes an isolated Docker daemon rather than the host daemon. The DinD sidecar is privileged.

Colab runs as root on a managed VM with public internet access and no equivalent Docker firewall. Use scoped, short-lived credentials, keep backups private, and read the [security model](docs/SECURITY.md) before running untrusted code.

## Documentation and checks

- [Docker setup, credentials, lifecycle, and troubleshooting](docs/DOCKER.md)
- [Colab setup, persistence, and recovery](docs/COLAB.md)
- [Token Monitor configuration and accounting](docs/TOKEN_MONITOR.md)
- [Security boundaries and limitations](docs/SECURITY.md)

GitHub Actions runs repository tests and shell checks, isolated Token Monitor integration tests, and a Gitleaks history scan on pull requests and pushes to `main`. To run the repository tests locally:

```bash
python3 -m unittest discover -s tests -v
```

The local suite needs PyYAML; some tests also require optional dependencies or explicitly configured integration fixtures; see the guides for their scope.
