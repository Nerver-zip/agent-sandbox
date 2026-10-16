# Agent Sandbox

> **A disposable, Tailscale-accessible development container for autonomous coding agents.**
>
> Keep the homelab and other Docker stacks out of the agent's workspace. Keep only deliberate login state across resets.

Agent Sandbox starts a dedicated Debian SSH server as a hardened Docker Compose workload. It uses the **existing Tailscale installation on the Docker host**, so no extra tailnet node, login approval, or Tailscale state is created in the container. You can still SSH to your host normally on port `22`; connect to the sandbox as `agent` on port `2222`.

**This is not a VM or a security boundary against a kernel escape.** A process running as `agent` can inspect any credentials made available to that account and send data to public Internet services. Review [the security model](docs/SECURITY.md) before allowing untrusted autonomous work.

## What is included?

| Category | Included by default |
|---|---|
| Base | Debian 13 (Trixie); unprivileged `agent` user (UID 10001), `/home/agent/workspace` |
| Terminal | Zsh, [Oh My Zsh](https://ohmyz.sh/), `git` plugin, autosuggestions, syntax highlighting, custom Starship / Catppuccin Mocha, Fastfetch |
| Editor & developer tools | Neovim, tmux, eza, fzf, ripgrep, fd, jq, htop, Git, curl, Python, pipx, Node.js/npm, C/C++ toolchain, CMake |
| Docker environment | Docker CLI, Docker Compose v2, Buildx via isolated Docker-in-Docker sidecar (`dind`); no host socket exposure |
| Agent harnesses | [Codex CLI](https://github.com/openai/codex) (`codex`) and [Antigravity CLI](https://antigravity.google/docs/cli/install/) (`agy`) preinstalled at **image build time** |
| GitHub | Official `gh` CLI; optional narrowly scoped fine-grained PAT in a **read-only** file mount; built-in HTTPS Git credential helper |
| Access | Independent OpenSSH service on the host Tailscale IPv4, port `2222`; SSH **public-key authentication only** |
| Isolation | Separate Docker bridge, explicit firewall rules, isolated DinD sidecar (no host Docker socket), no personal home or production mounts, no root shell for agent, CPU/RAM/PID limits |
| Persistence | **SSH server host identity**, **Codex `auth.json`**, **GitHub PAT** only; all other agent files are disposable |

A Nerd Font is recommended **on your SSH client terminal** to render the Starship icons. The configuration is based on a Catppuccin Mocha powerline prompt. It intentionally omits local-only paths, Android SDK variables, secret-tool commands, and the Oh My Zsh `sudo` plugin.

### Use cases

- SSH into a disposable `tmux`/Neovim shell from a laptop or phone over Tailscale.
- Clone a repository, ask Codex or AGY to work on it, review changes, push a branch, and create a draft PR with `gh`.
- Test coding agents and their CLI integrations without installing them on the homelab host.
- Reset all clones, caches, third-party tools, and generated artifacts while keeping SSH trust and permitted login credentials.

## Repository layout

```text
agent-sandbox/
├── Dockerfile                     # Repeatable base image + bundled agent CLIs
├── compose.yaml                   # Container hardening, limits and four allowlisted mounts
├── sshd_config                    # SSH key auth and session restrictions
├── config/
│   ├── agent.zshrc                 # Oh My Zsh, syntax highlighting, autosuggestions, eza, fastfetch
│   ├── starship.toml              # Catppuccin Mocha powerline prompt
│   ├── codex.toml                 # File credential store; no Codex command history
│   └── tmux.conf                  # Mouse support and scrollback history
├── scripts/
│   ├── setup.sh                   # Create/migrate SSH identity, public key and credentials
│   ├── up.sh                      # Network + firewall preflight, then build and launch
│   ├── destroy.sh                 # Explicit full reset (including all persisted credentials)
│   ├── firewall.sh                # Docker bridge egress and ingress isolation
│   ├── verify.sh                  # Post-install checks
│   ├── gh-wrapper.sh              # Supply GH_TOKEN only to gh, not to login shell
│   ├── set-gh-token.sh            # Hidden prompt: install fine-grained PAT
│   ├── reset-gh-token.sh          # Clear the local GitHub PAT
│   ├── import-codex-auth.sh       # Optional existing Codex auth import
│   ├── reset-codex-auth.sh        # Clear local Codex auth
│   ├── install-packages.sh        # Host-admin-only temporary apt installs
│   └── container-entrypoint.sh    # Start SSHD without regenerating server key
├── docs/
│   ├── SECURITY.md
│   └── agent-sandbox.service.example
├── tests/test_project.py
├── ssh/authorized_keys            # Generated: client PUBLIC key
├── persistent/                   # Generated and gitignored; never in Docker image
│   ├── ssh/ssh_host_ed25519_key[.pub]  # Persistent server identity
│   ├── codex/auth.json                  # Persistent Codex sign-in
│   └── gh/token                         # Optional fine-grained PAT
└── .env                          # Generated: non-secret local deployment settings
```

## Requirements

On the **Docker host**, install rootful Docker Engine / Compose v2, Tailscale (already logged in), `iptables` with Docker's `DOCKER-USER` chain, `iproute2`, `sudo`, `ssh-keygen`, and Python 3. The scripts expect Docker's **iptables** backend and `tailscale0` on the Docker host itself, not rootless Docker, native nftables Docker firewall mode, a remote Docker context, or Docker Desktop.

The deployment uses a dedicated network `172.30.247.0/24` (`br-agentsbx`). Confirm it does not overlap any current network. Builds need public Internet access to Debian repositories, npm, Oh My Zsh, and the Google Antigravity installer.

## First installation

On the Tailscale-connected homelab host:

```bash
cd ~/prod
unzip agent-sandbox-final.zip
cd agent-sandbox

# Authorize the PUBLIC key of the computer from which you will SSH.
./scripts/setup.sh ~/agent-key.pub

# Install firewall rules BEFORE creating the container.
./scripts/up.sh
./scripts/verify.sh
```

The public key must be **on the machine where you run setup**. If the matching private key lives only on your Arch desktop, copy its public half first:

```bash
# Run on the DESKTOP:
scp ~/.ssh/id_ed25519.pub nerver@100.118.53.107:~/agent-key.pub
```

The script also accepts `~/.ssh/id_ed25519.pub` as the default path. **Do not copy private SSH keys** into the project. Only `ssh/authorized_keys` (the public key) is copied into the image. The SSHD server identity keypair is stored separately in `persistent/ssh/`.

From an authorized client on the tailnet:

```bash
ssh -p 2222 agent@100.118.53.107  # The sandbox
ssh nerver@100.118.53.107         # The normal homelab (port 22)
```

For convenience, add the following to the SSH **client's** `~/.ssh/config`:

```sshconfig
Host homelab
    HostName 100.118.53.107
    User nerver
    Port 22
    IdentityFile ~/.ssh/id_ed25519

Host agent
    HostName 100.118.53.107
    User agent
    Port 2222
    IdentityFile ~/.ssh/id_ed25519
```

Then use `ssh homelab` and `ssh agent`. These two destinations have separate SSH host-key identities. If this is a brand-new installation, compare the SSH fingerprint against `ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub` **on the trusted host** before accepting it on your client.

## Day-to-day usage

SSH into the sandbox and use its preinstalled tools:

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

The default login shell is Zsh; Fastfetch appears on interactive login. The Starship theme displays the container's **Debian** icon, `agent@agent-sandbox`, directory, Git branch/status, supported language versions, and time. `ls`, `ll`, `la` use Eza; `v` opens Neovim.

You may install user-local packages or harnesses in `~/.local/bin` or a user npm prefix. They are **lost** on recreate. For temporary system packages, run on the host:

```bash
./scripts/install-packages.sh clang-format lldb
```

To add a tool permanently, modify `Dockerfile`, rebuild using `./scripts/up.sh`, and verify. The `agent` user has **no sudo**; host-admin scripts deliberately stay on the host.

### Codex authentication (persistent)

Codex defaults to the `file` credentials store, `/home/agent/.codex/auth.json`. This path is bind-mounted **read-write** from `persistent/codex/auth.json`, which is UID `10001`, mode `0600`. The setup creates an unauthenticated `{}` file. When you sign in normally through `codex`, credentials and refreshed tokens are written to the same file and survive container recreation. Sessions, logs, custom configuration, shell history, and workspace files do **not** persist.

Or import a previously authenticated Codex account (the source should come from a machine you trust):

```bash
docker compose stop
./scripts/import-codex-auth.sh /path/to/codex/auth.json
./scripts/up.sh
```

To clear persisted Codex credentials:

```bash
docker compose stop
./scripts/reset-codex-auth.sh
./scripts/up.sh
```

**Security and compatibility:** `auth.json` is an agent-readable bearer credential, and all commands running as `agent` can read it. Some future Codex versions might change the location or update behavior (including rename/unlink operations incompatible with a file bind mount). This build expects current in-place writes; `codex logout` may not be able to unlink a bind-mounted file. Use the reset script for local removal and revoke tokens at the provider when required. The `history.persistence = "none"` setting is not a guarantee that a remote provider does not keep server-side history.

### GitHub CLI with a fine-grained PAT (persistent)

`gh` is preinstalled. A **dedicated fine-grained PAT** is the preferred way to authorize GitHub operations inside this untrusted workspace. Unlike `gh auth login --with-token`, fine-grained PATs are recommended for use through `GH_TOKEN` rather than an interactive credential store. The token stays in `persistent/gh/token`, with owner UID `10001`, mode `0400`; it is mounted read-only at `/opt/agent-secrets/gh_token`. A root-owned wrapper at `/usr/local/bin/gh` loads that token into `GH_TOKEN` **only when launching `gh`**, rather than exporting it to every login shell. The underlying official binary remains `/usr/bin/gh`.

1. Create a token at [GitHub Settings → Developer settings → Personal access tokens → Fine-grained tokens](https://github.com/settings/personal-access-tokens/new).
2. Set a **short expiration** and choose **Only select repositories**. Start with the single repository you want to delegate.
3. Use the smallest necessary **Repository permissions**:

   | Agent task | Fine-grained PAT permissions |
   |---|---|
   | Read a private repository | **Contents: Read** (plus automatically included Metadata: Read) |
   | Push a feature branch | **Contents: Read and write** |
   | Create or update pull requests | **Pull requests: Read and write** (plus Contents: Read and write when pushing branches) |
   | Edit GitHub Actions workflow files | **Workflows: Read and write** *only if deliberately required* |
   | Work with issues/comments | Add **Issues** permissions if a specific command needs them |

   For a read-only investigation, don't grant any write permissions. No `admin` rights and no organization-wide/all-repositories token.

4. Store it on the **host** using the hidden prompt (don't paste credentials into your shell command history, Dockerfile, `.env` or chat):

```bash
./scripts/set-gh-token.sh
```

5. Verify within the sandbox:

```bash
ssh agent
gh auth status
gh repo view OWNER/REPO
git clone https://github.com/OWNER/REPO.git
```

A Git system-level HTTPS credential helper invokes `gh auth git-credential`, so Git operations against `github.com` can use the token when necessary without SSH private keys in the container. The token file can be populated before or after first startup, because it is mounted read-only from the host. `gh auth status` is an online check and certain commands may need more permissions than the examples above.

**Reset/rotate:** Revoke the old token in GitHub Settings, then use `./scripts/set-gh-token.sh` again; or run `./scripts/reset-gh-token.sh` to clear the local file. The local wrapper cannot protect the token from an agent that can read its own `/opt/agent-secrets/gh_token`; read-only means it cannot overwrite the host copy, **not** that it cannot copy or exfiltrate it. Prefer a GitHub App with short-lived installation tokens for more stringent environments.

### Antigravity CLI (`agy`)

AGY is installed during the image build using Google's official Linux installer, into the image's `/home/agent/.local/bin/agy`. Launch it with `agy`. Authentication on SSH typically provides a URL to open on your own device; complete the Google sign-in there and return to the terminal. **The AGY session is disposable**: this project does not mount its keyring or account cache. Expect to authenticate again after `docker compose down`. Future versions could offer a narrower, documented file/token storage mechanism.

### Docker and Docker Compose (Isolated DinD)

The workspace container includes `docker`, `docker compose`, and `docker buildx`. Rather than mounting the host's `/var/run/docker.sock` (which would allow an agent to inspect, control, or compromise other homelab containers on the host), Agent Sandbox connects to an isolated **Docker-in-Docker sidecar** (`dind:2375`) on the private sandbox bridge.

- The agent has full Docker capabilities inside its sandbox (e.g. `docker run`, `docker compose up`, building and testing containerized apps).
- The agent **cannot** see, inspect, stop, or tamper with any homelab or host containers.
- Outbound traffic from DinD and its containers inherits the host iptables firewall: public registry pulls and Internet connections work, while access to host services, LAN, tailnet, and other Docker bridges is blocked.
- Configurable resource limits in `.env` (`DIND_MEMORY`, `DIND_CPUS`).

## Update or migrate a previous Agent Sandbox

**Keep the old container running until `setup.sh` has migrated its SSH host key.** If you remove it first, its identity may be lost and your SSH client will warn about a host-key change.

1. Extract the new ZIP into a **temporary location**. Copy its source files (`Dockerfile`, `compose.yaml`, `scripts/`, `config/`, `docs/`, `tests/`, `.dockerignore`, `.gitignore`, `sshd_config`, README) into `~/prod/agent-sandbox`. Do **not** delete or replace the existing `.env`, `ssh/authorized_keys`, or `persistent/` directory.
2. With the **old** container still running, run `./scripts/setup.sh ~/agent-key.pub`. It reuses the current SSH server keypair if already persistent, imports it from the prior `ssh/host_keys/` layout if available, or copies it from the running old workspace. It can also import an existing Codex `auth.json` from the old running container.
3. Optionally run `./scripts/set-gh-token.sh` on the host.
4. Run `./scripts/up.sh` and `./scripts/verify.sh`, then reconnect from your client. The saved SSH host key means your `known_hosts` entry should remain valid.

**Recreating the container deletes its writable layer.** Back up or push any work you want to keep before upgrading. This is intentional.

## Lifecycle and persistence

| Operation | Agent code/files | SSH server host key | Codex sign-in | GitHub PAT |
|---|---|---|---|---|
| `docker compose stop` / `start` | Preserved | Preserved | Preserved | Preserved |
| `docker compose restart` | Preserved | Preserved | Preserved | Preserved |
| `docker compose down` → `./scripts/up.sh` | **Deleted** | Preserved | Preserved | Preserved |
| `./scripts/up.sh` recreates workspace | **Deleted** | Preserved | Preserved | Preserved |
| `./scripts/destroy.sh` | **Deleted** | **Deleted** | **Deleted** | **Deleted** |

Only the persistent SSH key, Codex auth, and optional GitHub PAT are deliberately mounted into the container. **No project directory, homelab folder, other container, or Docker socket is mounted**. The GitHub token remains in a host file; the Codex file is writable to support token refresh.

Normal lifecycle:

```bash
# Pause without deleting container data
docker compose stop
docker compose start

# Reset the ENTIRE agent filesystem but retain the three credential categories
docker compose down
./scripts/up.sh

# Destroy the sandbox, project-generated host state, logins and SSH identity
./scripts/destroy.sh
```

`docker compose down` does not remove the firewall's host iptables chains; `destroy.sh` removes them. Always use **`./scripts/up.sh`** (rather than plain `docker compose up`) to create/recreate the container: it configures the Docker network and the firewall first. `destroy.sh` does not remove *global Docker build caches, backups, snapshots or provider-side session data* and does not provide forensic erasure. It only targets its own named Compose resources, dedicated Docker network, image if unused, firewall chains and generated state. Other Docker stacks are not deliberately changed.

### Full reset and reinstall

```bash
./scripts/destroy.sh  # requires typing DESTROY
./scripts/setup.sh ~/agent-key.pub
./scripts/up.sh
```

A full destroy intentionally rotates the SSH server's identity. Verify the new fingerprint and remove the old `known_hosts` entry for **port 2222 only** after verification.

## Validation and troubleshooting

```bash
./scripts/verify.sh
docker compose ps
docker compose logs --tail=100 workspace
ssh -vvv -p 2222 agent@100.118.53.107
ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub
```

- **SSH authenticates but immediately disconnects:** review the SSHD logs, then verify `AUDIT_WRITE` is present in `cap_add` (needed for Linux SSHD audit events in this configuration).
- **`REMOTE HOST IDENTIFICATION HAS CHANGED`:** never disable SSH host-key checks to suppress this. Compare fingerprints with the trusted host. `destroy.sh` rotates the key on purpose.
- **No connection from an actual Tailscale client:** confirm Tailscale ACLs, the host's Tailscale IPv4 in `.env`, firewall, and `tailscale0`. A connection from the host to its *own* Tailscale IP is not proof of remote tailnet ACL enforcement.
- **Token/auth trouble:** run `stat -c '%u:%a' persistent/codex/auth.json persistent/gh/token` on the host (expect `10001:600` and `10001:400`); validate GitHub token permissions/expiration.
- **Glyph squares in the prompt:** choose a Nerd Font in your SSH client terminal. Do not copy font files to the container.
- **Codex cannot use its nested sandbox:** user namespaces/seccomp may prevent nesting a second Linux sandbox inside hardened Docker. Consider running inside a dedicated VM for workloads requiring strict nested sandboxing; don't weaken host Docker permissions casually.
- **Can't build:** check public network access, npm installer version requirements, and the official AGY installer endpoint. The base installer is upstream-controlled and should be reviewed before trusting it.

Static checks (with Python 3, PyYAML):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

`verify.sh` checks live mounts, capabilities, package presence, SSH key consistency and host firewall presence. **Neither test suite establishes VM-grade isolation.** Perform real negative tests against your homelab from the sandbox before entrusting it with sensitive production tasks.

## Security and reproducibility

This build uses a default `CODEX_VERSION=latest` npm install and upstream Oh My Zsh / AGY installers; **rebuilding later can install newer upstream versions**. Pin Codex via `CODEX_VERSION` in `.env`, and review the other upstream artifacts before considering the image reproducible. The firewall assumes Docker's iptables backend; host restarts or firewall reloads can remove its rules. Use the example systemd boot integration in `docs/` or equivalent ordering to reinstall the firewall before allowing the sandbox to run unattended.

**A hardened Docker container shares the kernel of the host.** If you expect agents to execute hostile code or want a robust isolation boundary, place this entire Docker workspace inside a dedicated VM/microVM with its own tailnet identity and egress policy. Read [docs/SECURITY.md](docs/SECURITY.md) for explicit assumptions and non-goals.

## References

- [Docker firewall and `DOCKER-USER`](https://docs.docker.com/engine/network/firewall-iptables/)
- [OpenAI Codex CLI](https://github.com/openai/codex)
- [Google Antigravity CLI installation](https://antigravity.google/docs/cli/install/)
- [GitHub CLI authentication](https://cli.github.com/manual/gh_auth_login) and [fine-grained PAT permissions](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
- [Starship](https://starship.rs/) · [Oh My Zsh](https://ohmyz.sh/) · [Fastfetch](https://github.com/fastfetch-cli/fastfetch)
