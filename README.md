# Agent Sandbox

A disposable development container for autonomous coding agents, accessible over Tailscale. It isolates coding workloads from other host containers and preserves only configured credentials and SSH keys across container recreations.

The sandbox runs OpenSSH inside a hardened Debian container via Docker Compose. It listens on the Docker host's existing Tailscale IP, avoiding separate node registration, approvals, or guest Tailscale processes. You connect to the host on port 22 and to the sandbox as `agent` on port 2222.

This setup does not provide virtual machine isolation or protect against kernel vulnerabilities. Code running as `agent` can read all credentials mounted into the account and access the public internet. See [docs/SECURITY.md](docs/SECURITY.md) for details on the security model and trust boundaries.

## Included components

| Category | Components |
|---|---|
| Base system | Debian 13 (Trixie), unprivileged `agent` user (UID 10001), `/home/agent/workspace` |
| Shell and terminal | Zsh, [Oh My Zsh](https://ohmyz.sh/), `git` plugin, autosuggestions, syntax highlighting, Starship prompt, Fastfetch |
| Development tools | Neovim, tmux, eza, fzf, ripgrep, fd, jq, htop, Git, curl, Python, pipx, Node.js/npm, C/C++ toolchain, CMake |
| Docker runtime | Docker CLI, Compose v2, and Buildx connected to an isolated Docker-in-Docker (`dind`) sidecar |
| Agent CLIs | [Codex CLI](https://github.com/openai/codex) (`codex`) and [Antigravity CLI](https://antigravity.google/docs/cli/install/) (`agy`) built into the image |
| GitHub integration | GitHub CLI (`gh`), optional fine-grained PAT mounted read-only, and HTTPS Git credential helper |
| Network access | OpenSSH service listening on the host Tailscale address at port 2222 with public-key authentication |
| Isolation controls | Dedicated Docker bridge, host firewall rules, unprivileged user, and resource limits (CPU, memory, PIDs) |
| Persistent state | Server host key, client `authorized_keys`, `known_hosts`, Codex `auth.json`, and GitHub PAT |

Install a Nerd Font on your terminal emulator to display the Starship prompt symbols correctly.

### Common tasks

- SSH into a fresh terminal session from a laptop, desktop, or mobile device over Tailscale.
- Clone a repository, run Codex or AGY, review generated changes, and push pull requests with `gh`.
- Test agent frameworks without installing their runtimes and dependencies directly on the host machine.
- Discard temporary repositories, builds, and caches after completing a task while preserving login sessions.

## Repository layout

```text
agent-sandbox/
├── Dockerfile                     # Base image build with preinstalled tools
├── compose.yaml                   # Hardened Compose configuration and allowlisted mounts
├── sshd_config                    # SSH daemon restrictions and authentication settings
├── config/
│   ├── agent.zshrc                # Shell configuration and alias definitions
│   ├── starship.toml              # Prompt layout and theme
│   ├── codex.toml                 # Codex CLI file credential storage settings
│   └── tmux.conf                  # Scrollback buffer and mouse configuration
├── scripts/
│   ├── setup.sh                   # SSH key generation, migration, and credential setup
│   ├── up.sh                      # Firewall verification, image build, and container startup
│   ├── destroy.sh                 # Environment reset and credential deletion
│   ├── firewall.sh                # Host iptables bridge isolation rules
│   ├── verify.sh                  # Configuration and health checks
│   ├── gh-wrapper.sh              # Scoped credential loader for the gh command
│   ├── set-gh-token.sh            # Prompt for storing a GitHub PAT
│   ├── reset-gh-token.sh          # Script to remove the stored GitHub PAT
│   ├── import-codex-auth.sh       # Script to import existing Codex credentials
│   ├── reset-codex-auth.sh        # Script to remove stored Codex credentials
│   ├── install-packages.sh        # Temporary package installation helper
│   └── container-entrypoint.sh    # Container initialization and Docker socket bridge
├── docs/
│   ├── SECURITY.md                # Security boundaries, firewall behavior, and risks
│   └── agent-sandbox.service.example
├── tests/test_project.py          # Static contract tests
├── ssh/authorized_keys            # Build-stage public keys
├── persistent/                    # Host-persisted files mounted at runtime
│   ├── ssh/ssh_host_ed25519_key[.pub]  # Server host keypair
│   ├── ssh/authorized_keys        # Allowed client public keys
│   ├── ssh/known_hosts            # Outbound SSH host verifications
│   ├── codex/auth.json            # Codex login credentials
│   └── gh/token                   # GitHub personal access token
└── .env                           # Local deployment variables
```

## Requirements

Run this project on a Linux machine with:
- Docker Engine and Docker Compose v2 (rootful)
- Active Tailscale connection on the host interface `tailscale0`
- `iptables` with Docker's `DOCKER-USER` chain enabled
- `iproute2`, `sudo`, `ssh-keygen`, and Python 3

The scripts require the standard iptables Docker backend on the host machine. Rootless Docker, native nftables mode, remote Docker contexts, and Docker Desktop are not supported.

The sandbox assigns the subnet `172.30.247.0/24` to a bridge named `br-agentsbx`. Ensure this range does not conflict with existing networks. Building the image requires internet access to fetch Debian packages, npm dependencies, Oh My Zsh, and the Antigravity installer.

## Installation

Run setup on the host where Docker and Tailscale are installed:

```bash
cd ~/prod/agent-sandbox

# Authorize your public key (or omit the path to use ~/.ssh/id_ed25519.pub)
./scripts/setup.sh ~/agent-key.pub

# Configure firewall rules and start the environment
./scripts/up.sh
./scripts/verify.sh
```

If your SSH key lives on a separate client machine, copy only the public key to the host first:

```bash
scp ~/.ssh/id_ed25519.pub nerver@100.118.53.107:~/agent-key.pub
```

Never copy private keys into the project. The setup script validates that inputs are public keys.

Connect from an authorized client on your tailnet:

```bash
ssh -p 2222 agent@100.118.53.107  # The sandbox
ssh nerver@100.118.53.107         # The host machine
```

To simplify connection commands, add entries to `~/.ssh/config` on your client machine:

```sshconfig
Host homelab
    HostName 100.118.53.107
    User nerver
    Port 22
    IdentityFile ~/.ssh/id_ed25519

Host agents
    HostName 100.118.53.107
    User agent
    Port 2222
    IdentityFile ~/.ssh/id_ed25519
```

You can then run `ssh homelab` or `ssh agents`. The host and container maintain separate SSH server host keys. On first connection, check the fingerprint against `ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub` on the host.

## Daily usage

Log in to the sandbox and run installed tools directly:

```bash
cd ~/workspace
fastfetch
codex --version
agy --version
gh --version
tmux new -s agents

# Start coding agents
codex
agy
```

Interactive sessions open in Zsh. Common aliases include `ls` for Eza and `v` for Neovim.

You can install packages into `~/.local/bin` or an npm user prefix during a session, but these files disappear when the container is recreated. To add temporary packages to a running container, execute the helper script on the host:

```bash
./scripts/install-packages.sh clang-format lldb
```

To add dependencies permanently, edit `Dockerfile` and rebuild with `./scripts/up.sh`. The `agent` account inside the container has no sudo permissions.

### Codex authentication

Codex stores login data in `/home/agent/.codex/auth.json`. The sandbox mounts this file read-write from `persistent/codex/auth.json` on the host, with ownership set to UID 10001 and permissions to `0600`. The setup script creates an empty JSON object (`{}`) if no file exists. When you run `codex` and sign in, token updates write to this file and persist across container restarts. Other files in `.codex`, such as logs, command caches, and session history, do not persist.

To import an existing `auth.json` file from another machine:

```bash
docker compose stop
./scripts/import-codex-auth.sh /path/to/codex/auth.json
./scripts/up.sh
```

To delete stored Codex credentials:

```bash
docker compose stop
./scripts/reset-codex-auth.sh
./scripts/up.sh
```

The `auth.json` file is accessible to any command executed by the `agent` user. In-place file updates work with the bind mount, but commands like `codex logout` that attempt to unlink the file directly may fail. Use `reset-codex-auth.sh` to clear local data and revoke sessions through OpenAI if a token is compromised.

### GitHub credentials

The image includes the GitHub CLI (`gh`). Using a dedicated fine-grained personal access token (PAT) restricts the agent's access to specified repositories. The token is stored on the host in `persistent/gh/token` (owned by UID 10001, mode `0400`) and mounted read-only at `/opt/agent-secrets/gh_token`. A wrapper at `/usr/local/bin/gh` sets `GH_TOKEN` only during CLI execution instead of exporting it across the user's login shell.

1. Generate a token in GitHub under personal access tokens (fine-grained tokens).
2. Choose a short expiration date and restrict repository access to the target project.
3. Grant only permissions necessary for the task:

   | Task | Required permissions |
   |---|---|
   | Inspect a repository | Contents: Read |
   | Push code branches | Contents: Read and write |
   | Open pull requests | Pull requests: Read and write (and Contents: Read and write) |
   | Update GitHub Actions | Workflows: Read and write |
   | Manage issues | Issues: Read and write |

4. Save the token on the host using the input prompt:

```bash
./scripts/set-gh-token.sh
```

5. Test access inside the container:

```bash
ssh agents
gh auth status
gh repo view OWNER/REPO
git clone https://github.com/OWNER/REPO.git
```

Git uses `gh auth git-credential` to authenticate HTTPS operations on `github.com`, allowing clones and pushes without placing SSH private keys inside the container.

To rotate or remove the token, revoke it in GitHub and run `./scripts/set-gh-token.sh` to write the replacement or `./scripts/reset-gh-token.sh` to delete the local file. The read-only mount prevents the container from editing the file on the host, but processes running as `agent` can still read and transmit the token value.

### Antigravity CLI

The Antigravity CLI (`agy`) is installed in `/home/agent/.local/bin/agy`. To authenticate, run `agy` and follow the URL displayed in the terminal to complete Google account verification in a browser. Session tokens are kept in the container's temporary filesystem and are cleared when the container is recreated with `docker compose down`.

### Docker and Compose

The workspace image includes the Docker CLI, Compose v2, and Buildx. The container connects over the internal network to a Docker-in-Docker sidecar (`dind:2375`) and forwards `/var/run/docker.sock` to that daemon using an internal socket proxy.

- The agent can run containers, build images, and test Compose stacks inside its own sandbox.
- The host Docker daemon is not mounted, so the agent cannot inspect or modify other containers running on the host.
- Outbound traffic from containers running inside DinD passes through the host firewall. Public downloads succeed, while access to the host, local networks, and Tailscale peers is blocked.
- Resource limits for the sidecar can be adjusted in `.env` via `DIND_MEMORY` and `DIND_CPUS`.

## Upgrading and migrations

Leave the existing container running while you run `setup.sh` so the script can preserve its SSH server host key and credentials.

1. Unpack updated files into a temporary directory, then copy source files (`Dockerfile`, `compose.yaml`, `scripts/`, `config/`, `docs/`, `tests/`, `sshd_config`, `README.md`) into your installation folder. Preserve existing `.env`, `ssh/authorized_keys`, and `persistent/` directories.
2. Run `./scripts/setup.sh`. The script reuses persistent host keys, imports existing keys from previous directory layouts, or pulls them from the running container. It also preserves existing Codex credentials.
3. Update the GitHub token if needed with `./scripts/set-gh-token.sh`.
4. Rebuild and restart with `./scripts/up.sh`, then run `./scripts/verify.sh`.

Push or back up workspace changes before updating, as recreating the container resets its writable filesystem.

## Container lifecycle

| Operation | Workspace files | Server host key | Client authorized keys | Codex credentials | GitHub PAT |
|---|---|---|---|---|---|
| `docker compose stop` / `start` | Preserved | Preserved | Preserved | Preserved | Preserved |
| `docker compose restart` | Preserved | Preserved | Preserved | Preserved | Preserved |
| `docker compose down` → `./scripts/up.sh` | Cleared | Preserved | Preserved | Preserved | Preserved |
| `./scripts/up.sh` (recreate) | Cleared | Preserved | Preserved | Preserved | Preserved |
| `./scripts/destroy.sh` | Cleared | Deleted | Deleted | Deleted | Deleted |

Only files in `persistent/` are mounted into the container. Host project folders, other container volumes, and host sockets remain unmapped.

Standard management commands:

```bash
# Pause and resume without resetting container data
docker compose stop
docker compose start

# Reset the container filesystem while preserving credentials and SSH keys
docker compose down
./scripts/up.sh

# Remove the sandbox, credentials, firewall rules, and generated state
./scripts/destroy.sh
```

`docker compose down` stops the containers but leaves firewall rules active. To restart the environment, use `./scripts/up.sh` so the network and firewall rules are verified before the container starts.

Running `destroy.sh` deletes the SSH host key, rotating the server identity. After running it, update your local `known_hosts` file for port 2222 before reconnecting.

## Troubleshooting and verification

Run inspection commands on the host:

```bash
./scripts/verify.sh
docker compose ps
docker compose logs --tail=100 workspace
ssh -vvv -p 2222 agent@100.118.53.107
ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub
```

Common issues:
- SSH connects and disconnects immediately: Check SSHD logs and ensure `AUDIT_WRITE` is included under `cap_add` in `compose.yaml`.
- Host key warning on reconnect: Verify the fingerprint matches `persistent/ssh/ssh_host_ed25519_key.pub`. Running `destroy.sh` generates a new key.
- Connection failures over Tailscale: Confirm the Tailscale IP in `.env`, firewall rules on `tailscale0`, and tailnet ACLs for the client device.
- Credential errors: Check host file permissions with `stat -c '%u:%a' persistent/codex/auth.json persistent/gh/token` (expected: `10001:600` and `10001:400`).
- Missing icons in prompt: Select a Nerd Font in your terminal emulator settings.
- Build failures: Check outbound network access to npm and the Antigravity installation endpoints.

To run the static test suite:

```bash
python3 -m unittest discover -s tests -v
```

The script `verify.sh` checks mounts, capabilities, tool availability, and host firewall rules. Test network restrictions directly from inside the container before running untrusted workloads.

## Security considerations

The image installs the latest versions of Codex, Oh My Zsh, and the Antigravity CLI at build time. To pin a specific Codex release, set `CODEX_VERSION` in `.env`.

Host reboots or firewall reloads can flush iptables rules. For unattended restarts, install the example systemd unit in `docs/agent-sandbox.service.example` to ensure `up.sh` runs and restores firewall rules before the containers start.

Containers share the host kernel. For untrusted code execution requiring strong isolation, run this project inside a dedicated virtual machine with its own Tailscale identity. Details are documented in [docs/SECURITY.md](docs/SECURITY.md).

## References

- [Docker firewall and DOCKER-USER](https://docs.docker.com/engine/network/firewall-iptables/)
- [OpenAI Codex CLI](https://github.com/openai/codex)
- [Google Antigravity CLI installation](https://antigravity.google/docs/cli/install/)
- [GitHub CLI authentication](https://cli.github.com/manual/gh_auth_login) and [fine-grained PAT permissions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [Starship](https://starship.rs/)
- [Oh My Zsh](https://ohmyz.sh/)
- [Fastfetch](https://github.com/fastfetch-cli/fastfetch)
