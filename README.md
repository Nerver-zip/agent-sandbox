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
| Agent CLIs | [Codex CLI](https://github.com/openai/codex) (`codex`), [Claude Code](https://github.com/anthropics/claude-code) (`claude`), [Antigravity CLI](https://antigravity.google/docs/cli/install/) (`agy`), and [OpenCode](https://opencode.ai/) (`opencode`) built into the image |
| GitHub integration | GitHub CLI (`gh`), optional fine-grained PAT mounted read-only, and HTTPS Git credential helper |
| Secret scanning | Pinned [gitleaks](https://github.com/gitleaks/gitleaks) release with checksum verification in the Colab bootstrap |
| Network access | OpenSSH service listening on the host Tailscale address at port 2222 with public-key authentication |
| Isolation controls | Dedicated Docker bridge, host firewall rules, unprivileged user, and resource limits (CPU, memory, PIDs) |
| Persistent state | Server host key, client `authorized_keys`, `known_hosts`, Codex `auth.json`, Claude `.credentials.json`, GitHub PAT, and Git config |
| Google Colab backend | Optional official-CLI provisioned CPU/GPU/TPU runtime with the same toolchain, no Docker required |

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
│   ├── codex.toml                 # Codex CLI settings (reasoning, TUI statusline, subagents)
│   ├── tmux.conf                  # Scrollback, mouse, focus events, passthrough, bell, and reload shortcut
│   ├── nvim/                      # Minimal Neovim setup (Catppuccin, Telescope, Nvim-Tree)
│   ├── claude/                    # Claude Code configuration, preferences, and statusline
│   ├── gemini/                    # Antigravity CLI preferences and Catppuccin theme
│   └── opencode/                  # OpenCode config and agent-notify plugin
├── scripts/
│   ├── setup.sh                   # SSH key generation, migration, and credential setup
│   ├── up.sh                      # Firewall verification, image build, and container startup
│   ├── destroy.sh                 # Environment reset and credential deletion
│   ├── firewall.sh                # Host iptables bridge isolation rules
│   ├── verify.sh                  # Configuration and health checks
│   ├── gh-wrapper.sh              # Scoped credential loader for the gh command
│   ├── agent-notify.sh            # Installed as agent-notify: end-of-turn alerts to the SSH client
│   ├── set-gh-token.sh            # Prompt for storing a GitHub PAT
│   ├── reset-gh-token.sh          # Script to remove the stored GitHub PAT
│   ├── import-codex-auth.sh       # Script to import existing Codex credentials
│   ├── reset-codex-auth.sh        # Script to remove stored Codex credentials
│   ├── import-claude-auth.sh      # Script to import existing Claude Code credentials
│   ├── reset-claude-auth.sh       # Script to remove stored Claude Code credentials
│   ├── import-git-config.sh       # Script to import Git author identity (.gitconfig or name/email)
│   ├── reset-git-config.sh        # Script to reset stored Git author identity
│   ├── install-packages.sh        # Temporary package installation helper
│   ├── container-entrypoint.sh    # Container initialization and Docker socket bridge
│   └── colab/                     # Optional Google Colab backend
│       ├── setup.sh               # Local CLI/key/config setup
│       ├── up.sh                  # Provision and bootstrap one runtime
│       ├── connect.sh             # OpenSSH session through the Colab proxy
│       ├── status.sh              # Runtime, bootstrap, and compute-unit status
│       ├── down.sh                # Release the tracked runtime
│       ├── save.sh                # Archive the remote workspace locally
│       ├── restore.sh             # Restore an archive into a runtime
│       ├── cache.sh               # Inspect, prune, invalidate, or clear the cache
│       ├── cache-remote.sh        # Allowlisted remote cache capture/restore
│       ├── set-gh-token.sh        # Send an optional PAT over stdin
│       ├── bootstrap.sh           # Idempotent in-runtime installer
│       ├── proxy.sh               # Auto-create-safe ProxyCommand guard
│       └── _common.sh             # Shared shell helpers
├── docs/
│   ├── SECURITY.md                # Security boundaries, firewall behavior, and risks
│   ├── COLAB.md                   # Colab architecture, parity, persistence, and troubleshooting
│   └── agent-sandbox.service.example
├── tests/
│   ├── test_project.py            # Static Docker contract tests
│   ├── test_codex_auth_storage.py # Codex credential storage regressions
│   └── test_colab.py              # Colab lifecycle and command-construction tests
├── ssh/authorized_keys            # Build-stage public keys
├── persistent/                    # Host-persisted files mounted at runtime
│   ├── ssh/ssh_host_ed25519_key[.pub]  # Server host keypair
│   ├── ssh/authorized_keys        # Allowed client public keys
│   ├── ssh/known_hosts            # Outbound SSH host verifications
│   ├── codex/auth.json            # Codex login credentials
│   ├── codex/auth.lock            # Codex refresh serialization lock
│   ├── claude/.credentials.json   # Claude Code OAuth credentials
│   ├── gh/token                   # GitHub personal access token
│   └── git/config                 # Git user configuration (name and email)
├── .colab/                        # Local Colab state; ignored and never committed
├── backups/                       # Local Colab workspace archives; ignored
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
# Optionally pass a .gitconfig file to import your Git identity
./scripts/setup.sh ~/agent-key.pub ~/client.gitconfig

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

## Google Colab backend

An optional backend provisions a disposable Google Colab runtime and installs the same toolchain there. It is independent of the Docker deployment and requires no Docker, Tailscale, inbound port, or privileged container. The runtime is addressed through the official Colab CLI's WebSocket SSH proxy.

Prerequisites: a Google account with Colab access and a Linux or macOS client with OpenSSH and Git. `setup.sh` installs `google-colab-cli` with `uv` or `pipx` when it is missing.

```bash
./scripts/colab/setup.sh   # one-time local setup, dedicated key, auth check
./scripts/colab/up.sh      # create a CPU runtime and bootstrap the tools
./scripts/colab/connect.sh # interactive Zsh in ~/workspace
./scripts/colab/status.sh  # runtime state, tools, compute units, local cache
./scripts/colab/save.sh    # archive the remote workspace locally
./scripts/colab/test.sh    # run this repository's test suite on the runtime
./scripts/colab/down.sh    # release the runtime
./scripts/colab/cache.sh   # inspect, prune, invalidate, or clear the tool cache
```

- CPU is the default. Request `--gpu T4`, `--tpu v5e1`, or `--high-mem` only for a new runtime. Release the current runtime before changing hardware.
- `up.sh` reuses an existing runtime, skips a current bootstrap, never retries a failed allocation, and refuses to create a second runtime during recovery. See `docs/COLAB.md` for the exact safety rules.
- The bootstrap installs independent components concurrently, cutting the cold bootstrap from 148 s to about 95 s in the live benchmark. A small, validated local cache of Neovim plugin data only is available with `./scripts/colab/up.sh --cache`; it is off by default because the measured restore was slower than the parallel install path on a normal Colab connection. Large npm/Node artifacts are deliberately not cached because the SSH transfer is slower than Colab's direct package downloads; see the benchmark table in `docs/COLAB.md`.
- `up.sh --restore-workspace` restores the newest local workspace archive after the environment is ready. It replaces the live workspace only when that workspace is empty; otherwise it extracts to a new directory.
- `connect.sh` is a normal OpenSSH session through `colab ssh --proxy-mode`; `scp`, `sftp`, tmux, and IDE remote-SSH work. Use `connect.sh -- command` for one-shot commands and `--force` to terminate stale local `colab ssh` proxy processes. It cannot disconnect a client running on another machine.
- Authenticate agents interactively inside the runtime (`codex login --device-auth`, `claude`, `agy`, `opencode auth login`, `gh auth login`). Host tokens are never copied automatically. `set-gh-token.sh` can send an optional fine-grained PAT over stdin.
- The runtime login is `root`, `/home/agent/workspace` is a compatibility path, and credentials and the workspace disappear when the runtime is released or expires. Push to Git and use `save.sh` before releasing.
- There is no Docker-in-Docker, persistent remote filesystem, Tailscale identity, or unprivileged user in this backend. The Docker backend remains the choice for container workflows.
- Detailed architecture, cache design, feature parity, persistence, security, configuration, benchmarks, and troubleshooting: [docs/COLAB.md](docs/COLAB.md).

The Docker deployment below is unchanged and does not depend on the Colab state under `.colab/`.

## Daily usage

Log in to the sandbox and run installed tools directly:

```bash
cd ~/workspace
fastfetch
codex --version
claude --version
agy --version
opencode --version
gh --version
tmux new -s agents

# Start coding agents
codex
claude
agy
opencode
```

Interactive sessions open in Zsh. Common aliases include `ls` for Eza and `v` for Neovim.

Neovim includes a minimal setup with essential plugins:
- Theme: Catppuccin Mocha (matching Starship and the terminal palette)
- File explorer: `nvim-tree` (toggle with `<C-n>`, focus with `<leader>e`)
- Fuzzy finder: `telescope` (find files with `<leader>ff`, grep with `<leader>fw`, buffers with `<leader>fb`)
- Syntax and status: Treesitter, Lualine, and Gitsigns
- Keymaps: `jk` and `kj` to exit insert mode, `;` for command mode, and `<C-h/j/k/l>` for split navigation

You can install packages into `~/.local/bin` or an npm user prefix during a session, but these files disappear when the container is recreated. To add temporary packages to a running container, execute the helper script on the host:

```bash
./scripts/install-packages.sh clang-format lldb
```

To add dependencies permanently, edit `Dockerfile` and rebuild with `./scripts/up.sh --rebuild`. The `agent` account inside the container has no sudo permissions.

### Codex authentication

Codex keeps its normal `CODEX_HOME` at `/home/agent/.codex`. That directory stays in the disposable container layer. The entrypoint links `auth.json` and Codex's `auth.lock` into `/home/agent/.codex-auth`, which is a directory bind-mounted from `persistent/codex` on the host. The host directory is owned by UID 10001 with mode `0700`; `auth.json` and `auth.lock` are owned by UID 10001 with mode `0600`.

This directory mount lets current Codex releases create a private `.auth-*` temporary file, sync it, and atomically rename it over `auth.json`. A file bind mount blocks that rename with `EBUSY`. Codex login and logout also remove the `auth.json` pathname directly, so `/usr/local/bin/codex` is a small wrapper around the installed CLI: it synchronizes a newly created auth file back into the persistent directory, recreates the symlink, and makes logout durable. If a login or logout is interrupted, the entrypoint completes the sign-out before relinking the store. Config, logs, cache, history, sessions, and workspace remain ephemeral.

To import an existing `auth.json` file from another machine:

```bash
./scripts/stop.sh
./scripts/import-codex-auth.sh /path/to/codex/auth.json
./scripts/up.sh
```

To delete stored Codex credentials:

```bash
./scripts/stop.sh
./scripts/reset-codex-auth.sh
./scripts/up.sh
```

The credential file is accessible to any command executed by the `agent` user. The wrapper handles Codex's unlink-before-login and logout behavior while keeping the persistent file in the mounted directory. Use `reset-codex-auth.sh` when you need to clear local credentials without launching Codex; revoke compromised sessions through OpenAI as well.

To validate the live device-code flow after deploying, run:

```bash
docker compose exec -it --user agent workspace codex login --device-auth
docker compose exec -T --user agent workspace codex login status
docker compose exec -T --user agent workspace sh -lc '
  test "$CODEX_HOME" = /home/agent/.codex &&
  test "$(readlink "$CODEX_HOME/auth.json")" = "$CODEX_AUTH_DIR/auth.json" &&
  stat -c "%u:%a" "$CODEX_AUTH_DIR" "$CODEX_AUTH_DIR/auth.json" "$CODEX_AUTH_DIR/auth.lock"
'
```

The command output shows only paths, ownership, and modes; do not print or copy `auth.json`. A successful Codex request exercises the normal refresh path when the service determines a refresh is due. The automated Docker regression uses synthetic credentials and repeated `fsync`/rename operations, so it does not need a real account.

### Claude Code authentication

Claude Code credentials are saved in `/home/agent/.claude/.credentials.json`. The sandbox mounts this file from `persistent/claude/.credentials.json` on the host, owned by UID 10001 with file mode `0600`. When you run `claude` and complete OAuth, the credentials write to this file and persist across container recreations. Ephemeral files such as project session logs, shell snapshots, and debug traces do not persist.

The image includes generic preferences in `/home/agent/.claude/settings.json`, `/home/agent/.claude/CLAUDE.md`, and a Catppuccin Mocha statusline in `/home/agent/.claude/statusline.sh`.

To import existing Claude credentials:

```bash
./scripts/stop.sh
./scripts/import-claude-auth.sh /path/to/.credentials.json
./scripts/up.sh
```

To delete stored Claude credentials:

```bash
./scripts/stop.sh
./scripts/reset-claude-auth.sh
./scripts/up.sh
```

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

### Git identity

The setup script automatically detects the host machine's `user.name` and `user.email` from Git and writes them to `persistent/git/config`. If you deploy from a remote client machine where your Git configuration lives, pass your `.gitconfig` to `setup.sh`:

```bash
scp ~/.gitconfig nerver@100.118.53.107:~/client.gitconfig
./scripts/setup.sh ~/agent-key.pub ~/client.gitconfig
```

To import an existing `.gitconfig` from another machine at any time:

```bash
./scripts/stop.sh
./scripts/import-git-config.sh /path/to/.gitconfig
./scripts/up.sh
```

You can also import directly with a name and email:

```bash
./scripts/stop.sh
./scripts/import-git-config.sh "Marcelo Augusto" "marceloaugusto12161@gmail.com"
./scripts/up.sh
```

To delete stored Git identity and reset to default:

```bash
./scripts/stop.sh
./scripts/reset-git-config.sh
./scripts/up.sh
```

This directory is mounted at `/home/agent/.config/git`, allowing `git commit` and `git config --global` to operate directly inside the container without manual configuration. Changes made with `git config --global` inside the sandbox persist across container recreations.

### OpenCode

OpenCode (`opencode`) is installed from the `opencode-ai` npm package; pin a release with `OPENCODE_VERSION` in `.env`. The image ships `/home/agent/.config/opencode/opencode.json` (Catppuccin theme, auto-update off) and a global plugin, `plugins/agent-notify.js`, that calls `agent-notify opencode` when a session goes idle. Run `opencode` inside tmux and sign in with `opencode auth login`. Credentials live under `~/.local/share/opencode` in the container's temporary filesystem and are cleared when the container is recreated.

### Antigravity CLI

The Antigravity CLI (`agy`) is installed in `/home/agent/.local/bin/agy`. To authenticate, run `agy` and follow the URL displayed in the terminal to complete Google account verification in a browser. Session tokens are kept in the container's temporary filesystem and are cleared when the container is recreated with `docker compose down`.

### Docker and Compose

The workspace image includes the Docker CLI, Compose v2, and Buildx. The container connects over the internal network to a Docker-in-Docker sidecar (`dind:2375`) and forwards `/var/run/docker.sock` to that daemon using an internal socket proxy.

- The agent can run containers, build images, and test Compose stacks inside its own sandbox.
- The host Docker daemon is not mounted, so the agent cannot inspect or modify other containers running on the host.
- Outbound traffic from containers running inside DinD passes through the host firewall. Public downloads succeed, while access to the host, local networks, and Tailscale peers is blocked.
- Resource limits for the sidecar can be adjusted in `.env` via `DIND_MEMORY` and `DIND_CPUS`.

## Upgrading and migrations

Leave the existing container running while you run `setup.sh` so the script can preserve its SSH server host key and credentials. The Codex migration keeps the existing `persistent/codex/auth.json` file in place and changes the host directory to UID 10001, mode `0700`; it does not replace the auth contents.

1. Unpack updated files into a temporary directory, then copy source files (`Dockerfile`, `compose.yaml`, `scripts/`, `config/`, `docs/`, `tests/`, `sshd_config`, `README.md`) into your installation folder. Preserve existing `.env`, `ssh/authorized_keys`, and `persistent/` directories.
2. Run `./scripts/setup.sh`. The script reuses persistent host keys, imports existing keys from previous directory layouts, or pulls them from the running container. It preserves the existing Codex auth file and prepares the new credential-directory mount.
3. Update the GitHub token if needed with `./scripts/set-gh-token.sh`.
4. Back up or commit workspace changes, then recreate the container with `./scripts/up.sh --rebuild` and run `./scripts/verify.sh`. Recreating the container clears workspace files but keeps all configured host-persisted credentials and keys.

Push or back up workspace changes before updating, as recreating the container resets its writable filesystem.

## Token Monitor

Optional [Token Monitor accounting](docs/TOKEN_MONITOR.md) reports Claude Code, Codex, OpenCode and Antigravity CLI usage to an independent Hub. Configure it once on the Docker host to preserve usage across sandbox resets. Use the lifecycle scripts below so they can capture final usage before removing the workspace. Antigravity collection remains under review; see the guide's security blocker before enabling it.

## Container lifecycle

| Operation | Workspace files | Server host key | Client authorized keys | Codex credentials | Claude credentials | GitHub PAT | Git config |
|---|---|---|---|---|---|---|---|
| `./scripts/stop.sh` → `./scripts/up.sh` | Preserved | Preserved | Preserved | Preserved | Preserved | Preserved | Preserved |
| `docker compose restart` | Preserved | Preserved | Preserved | Preserved | Preserved | Preserved | Preserved |
| `./scripts/stop.sh --down` → `./scripts/up.sh` | Cleared | Preserved | Preserved | Preserved | Preserved | Preserved | Preserved |
| `./scripts/up.sh --rebuild` | Cleared | Preserved | Preserved | Preserved | Preserved | Preserved | Preserved |
| `./scripts/destroy.sh` | Cleared | Deleted | Deleted | Deleted | Deleted | Deleted | Deleted |

Only files in `persistent/` are mounted into the container. Host project folders, other container volumes, and host sockets remain unmapped.

Standard management commands:

```bash
# Pause and resume without resetting container data (restores firewall on resume)
./scripts/stop.sh
./scripts/up.sh

# Reset the container filesystem while preserving credentials and SSH keys
./scripts/stop.sh --down
./scripts/up.sh
# Or in a single command:
./scripts/up.sh --rebuild

# Remove the sandbox, credentials, firewall rules, and generated state
./scripts/destroy.sh
```

`./scripts/stop.sh --down` stops and removes the containers and leaves firewall rules active. To restart the environment, use `./scripts/up.sh` so the network and firewall rules are verified before the container starts.

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
- Credential errors: Check the Codex store with `docker compose exec --user agent workspace stat -c '%u:%a' /home/agent/.codex-auth /home/agent/.codex-auth/auth.json /home/agent/.codex-auth/auth.lock` (expected: `10001:700`, `10001:600`, and `10001:600`).
- Missing icons in prompt: Select a Nerd Font in your terminal emulator settings.
- Build failures: Check outbound network access to npm and the Antigravity installation endpoints.
- Colab backend: Run `./scripts/colab/status.sh` first, then see the troubleshooting section in [docs/COLAB.md](docs/COLAB.md#troubleshooting).

To run the static test suite:

```bash
python3 -m unittest discover -s tests -v
```

The script `verify.sh` checks mounts, capabilities, tool availability, and host firewall rules. Test network restrictions directly from inside the container before running untrusted workloads.

## Security considerations

The image installs the latest versions of Codex, Oh My Zsh, and the Antigravity CLI at build time. To pin a specific Codex or OpenCode release, set `CODEX_VERSION` or `OPENCODE_VERSION` in `.env`.

Host reboots or firewall reloads can flush iptables rules. For unattended restarts, install the example systemd unit in `docs/agent-sandbox.service.example` to ensure `up.sh` runs and restores firewall rules before the containers start.

Containers share the host kernel. For untrusted code execution requiring strong isolation, run this project inside a dedicated virtual machine with its own Tailscale identity. Details are documented in [docs/SECURITY.md](docs/SECURITY.md).

## References

- [Docker firewall and DOCKER-USER](https://docs.docker.com/engine/network/firewall-iptables/)
- [Google Colab CLI](https://github.com/googlecolab/google-colab-cli)
- [OpenAI Codex CLI](https://github.com/openai/codex)
- [Google Antigravity CLI installation](https://antigravity.google/docs/cli/install/)
- [GitHub CLI authentication](https://cli.github.com/manual/gh_auth_login) and [fine-grained PAT permissions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [Starship](https://starship.rs/)
- [Oh My Zsh](https://ohmyz.sh/)
- [Fastfetch](https://github.com/fastfetch-cli/fastfetch)

## Client notifications

Claude Code, Codex, Antigravity, and OpenCode (through its `agent-notify.js` plugin) call `agent-notify` when a turn ends. The container has no display, so the script emits an OSC 99 escape sequence (kitty protocol) (wrapped in tmux DCS passthrough when inside tmux) that your SSH client terminal turns into a native desktop notification. It also rings the bell so tmux flags the window.

- Works over SSH with or without tmux, as long as the client terminal supports the protocol (kitty by default).
- Other terminals: set `AGENT_NOTIFY_OSC` to `777` (foot, Ghostty, WezTerm, Konsole), `9` (iTerm2, Windows Terminal), or `off`.
- If your client runs its own tmux, enable `set -g allow-passthrough on` there too.
- Rebuild the image (`./scripts/up.sh --rebuild`) to apply; existing agent sessions must be restarted.
