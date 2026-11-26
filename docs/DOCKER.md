# Docker backend

The Docker backend runs an OpenSSH server and an unprivileged `agent` account in a disposable Debian workspace. It binds SSH to the Docker host's Tailscale IPv4 address; it does not create a separate Tailscale device. A Docker-in-Docker sidecar provides a separate daemon for builds and containers. The host Docker socket and host project directories are not mounted into the workspace. See the [security model](SECURITY.md) for the limits of this isolation.

## Requirements and setup

Use a Linux host with rootful Docker Engine, Docker Compose v2, Tailscale connected on `tailscale0`, the standard iptables Docker backend and `DOCKER-USER` chain, `iproute2`, `sudo`, OpenSSH tools, Python 3, and Git. Rootless Docker, Docker Desktop, native Docker nftables mode, and remote Docker contexts are unsupported. The fixed `172.30.247.0/24` subnet and `br-agentsbx` bridge must not conflict with your network. The image build requires internet access to package registries and the Antigravity installer.

On the Docker host, from the checkout:

```bash
# Supply a public key or authorized_keys file. A second argument can import a Git config.
./scripts/setup.sh ~/.ssh/id_ed25519.pub
./scripts/up.sh
./scripts/verify.sh
```

If your public key is on another machine, copy only the public key to the host first. Setup can also discover existing authorized keys; pass a path explicitly when none is available. It creates `.env`, SSH identity and credential placeholders under `persistent/`, and records the host Tailscale address. It does not require copying your personal SSH private key.

Connect from an authorized tailnet client, replacing the hostname with the host's Tailscale address or DNS name:

```bash
ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub  # run on the host
ssh -p 2222 agent@your-tailscale-host                     # run on the client
```

Compare the fingerprint before accepting the host key. `.env` controls `AGENT_SSH_PORT`, workspace and DinD resource limits, and `CODEX_VERSION`, `CLAUDE_VERSION`, and `OPENCODE_VERSION`. See [.env.example](../.env.example) for defaults. The image uses the `opencode-ai` package; version choices are build inputs, and changing them requires a rebuild. Antigravity is installed by its upstream installer.

## Using the workspace

Sign in to each service inside the workspace and run its CLI:

```bash
codex login --device-auth
claude
agy
opencode auth login
```

The workspace is `~/workspace`. The image also includes Git, GitHub CLI, tmux, Neovim, Node.js, Python, and build tools. Session-installed packages and workspace files are disposable. Install temporary Debian packages from the host with `./scripts/install-packages.sh PACKAGE...`; add permanent image dependencies to the Dockerfile and rebuild. The agent account has no sudo access.

The image includes a configured Neovim, Zsh and Starship environment. In Neovim, `<C-n>` toggles the file tree, `<leader>ff` finds files, and `<leader>fw` searches text. The image also includes Telescope, Treesitter, Lualine, and Gitsigns. `agent-notify` sends end-of-turn notifications for the four agents over SSH using OSC 99 by default and rings the terminal bell. Set `AGENT_NOTIFY_OSC` to `777`, `9`, or `off` for other client terminals; client-side tmux may need `set -g allow-passthrough on`.

### Agent credentials

| Agent | Docker persistence | Import or reset on the host |
| --- | --- | --- |
| Codex | `persistent/codex/` holds the auth document and lock; the rest of `CODEX_HOME` is disposable. | `import-codex-auth.sh` / `reset-codex-auth.sh` |
| Claude Code | `persistent/claude/.credentials.json` persists; other Claude files are disposable. | `import-claude-auth.sh` / `reset-claude-auth.sh` |
| OpenCode | Credentials in the container's `~/.local/share/opencode` are disposable. | Authenticate again after recreation. |
| Antigravity | CLI session tokens are disposable. | Authenticate again after recreation. |

Stop the workspace before replacing persistent Codex or Claude credentials, then restart it:

```bash
./scripts/stop.sh
./scripts/import-codex-auth.sh /path/to/auth.json
# Or: ./scripts/import-claude-auth.sh /path/to/.credentials.json
./scripts/up.sh
```

The corresponding reset scripts remove the locally stored credentials. Revoke compromised sessions with the provider as well. The Codex store is a directory mount so atomic credential refresh and logout can work; do not replace it with a single-file bind mount. Agent processes can read credentials available to their account.

### GitHub and Git identity

To provide a scoped GitHub personal access token, create a short-lived fine-grained token with only the required repository permissions and enter it using the host prompt:

```bash
./scripts/set-gh-token.sh
```

The token is held in `persistent/gh/token` and mounted read-only. The `gh` wrapper supplies it to GitHub CLI, and Git uses the CLI credential helper for HTTPS. Read-only mounting does not prevent the agent from reading or transmitting the token. Rotate or revoke it at GitHub and use `set-gh-token.sh` or `reset-gh-token.sh` on the host.

Setup detects the host's Git name and email, or accepts a Git config as its second argument. To change the identity later:

```bash
./scripts/stop.sh
./scripts/import-git-config.sh /path/to/.gitconfig
# Or: ./scripts/import-git-config.sh "Example Developer" "developer@example.com"
./scripts/up.sh
```

`reset-git-config.sh` resets the stored identity. The Git configuration under `persistent/git/` survives recreation.

## Lifecycle and persistence

| Operation | Workspace and session files | Host SSH identity, Git config, Codex/Claude and configured GitHub credentials |
| --- | --- | --- |
| `./scripts/stop.sh` then `./scripts/up.sh` | Preserved | Preserved |
| `./scripts/stop.sh --down` then `./scripts/up.sh` | Removed with containers | Preserved |
| `./scripts/up.sh --rebuild` | Removed on recreation | Preserved |
| `./scripts/destroy.sh` | Removed | Deleted along with generated `.env` and SSH state |

OpenCode and Antigravity login files are part of the disposable container filesystem. Back up or push workspace changes before removal or rebuild. `stop.sh --down` leaves the firewall in place; use `up.sh` to verify firewall rules before starting again. `destroy.sh` prompts for confirmation, removes this project's containers, network, generated identity and credentials, and rotates the SSH server identity on a later setup. It does not purge the global Docker build cache or other Docker projects.

When [Token Monitor](TOKEN_MONITOR.md) is enabled, the lifecycle scripts attempt a final capture before removal. A failed final capture prevents normal removal; `--force` explicitly bypasses this protection. Token Monitor's host configuration, secret, archive and queued snapshots remain after `destroy.sh`; they need their own backup and retention policy. Raw `docker compose down` bypasses this capture.

## Upgrade and recovery

Before updating the checkout, push or back up workspace changes and protect `.env`, `ssh/authorized_keys`, `persistent/`, and any Token Monitor configuration and state. Review changes to those files rather than replacing them with defaults. If migrating an older installation, keep the current workspace running while running `./scripts/setup.sh`; setup can migrate missing SSH and credential state from a running legacy container and preserves existing host credentials. Check the result before running `./scripts/up.sh --rebuild`, which recreates the workspace. Run `./scripts/verify.sh` after startup. For an existing Token Monitor installation, follow its [in-place provider upgrade](TOKEN_MONITOR.md#enable-antigravity-on-an-existing-installation) separately; it does not require rebuilding the sandbox.

For diagnosis, run on the host:

```bash
./scripts/verify.sh
docker compose ps
docker compose logs --tail=100 workspace
ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub
```

Check the configured Tailscale address and tailnet ACL if SSH cannot connect. A host-key warning after destruction may be expected, but verify the new fingerprint before changing the client's `known_hosts`. If Codex authentication fails, inspect file ownership and modes without printing credentials. Check registry and installer access for build failures. A Nerd Font can improve prompt glyphs.

Host firewall or Docker daemon reloads can flush rules. The [systemd example](agent-sandbox.service.example) starts the sandbox after Docker and Tailscale; review and adapt it before unattended use. The [security guide](SECURITY.md) describes firewall assumptions and additional hardening.
