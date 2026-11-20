# Google Colab backend

The Colab backend runs the Agent Sandbox toolchain on a disposable Google Colab
VM instead of a local Docker container. It uses the official
[`google-colab-cli`](https://github.com/googlecolab/google-colab-cli) for
provisioning, authentication, compute-unit accounting, and the SSH transport.
The existing Docker deployment is untouched and remains the default backend.

## Architecture

```text
local terminal
  │
  ├── ./scripts/colab/up.sh ── colab CLI ── Colab assignment API
  │                                      └─ one-time bootstrap over SSH
  │
  └── ssh -F .colab/ssh/config agent-sandbox-colab
        │
        └── ProxyCommand: ./scripts/colab/proxy.sh
              ├── verifies the session still exists (never creates one)
              └── colab ssh --proxy-mode ── WSS ── Colab runtime sshd
                                                     └── root shell
                                                         └── /home/agent/workspace
```

Key decisions:

- A standard CPU runtime is the default. No nested container, privileged
  daemon, systemd unit, inbound port, or Tailscale node is required.
- The runtime is bootstrapped once over SSH. The bootstrap is idempotent and
  records a content hash; `up.sh` skips it when nothing changed.
- `connect.sh` uses a generated OpenSSH config and the official
  `colab ssh --proxy-mode` WebSocket bridge. `scp`, `sftp`, IDE remote-SSH, and
  normal OpenSSH options work through that config.
- The internal `proxy.sh` verifies the session before invoking the CLI. The
  Colab CLI auto-creates a runtime when a proxy session is missing, so this
  guard is what prevents an accidental second runtime (and an accidental
  billable allocation) during reconnects.

## Prerequisites

On the client (Linux or macOS):

- A Google account with Colab access.
- A shell, `ssh`, `ssh-keygen`, `git`, `tar`, `curl`, and `python3`.
- `uv` (recommended) or `pipx` if `google-colab-cli` is not installed.
  `setup.sh` installs the CLI locally when needed.
- A Nerd Font in the terminal emulator for Starship glyphs.

The client does not need Docker, Tailscale, sudo, or an inbound port.

## Quick start

```bash
# One-time local setup: installs the CLI if needed, creates a dedicated SSH
# key under .colab/, stores Git identity, and writes the OpenSSH alias.
./scripts/colab/setup.sh

# Provision a CPU runtime and install the Agent Sandbox tools inside it.
./scripts/colab/up.sh

# Open an interactive Zsh session in /home/agent/workspace.
./scripts/colab/connect.sh

# Inspect backend state, bootstrap freshness, tools, and compute units.
./scripts/colab/status.sh

# Archive the complete remote workspace locally.
./scripts/colab/save.sh

# Release the runtime (prompts unless --yes is passed).
./scripts/colab/down.sh
```

Optional accelerators are requested only when creating a new runtime:

```bash
./scripts/colab/up.sh --gpu T4
./scripts/colab/up.sh --tpu v5e1
./scripts/colab/up.sh --high-mem
```

`down.sh` must be run before changing the hardware of an existing runtime.
`up.sh` refuses to create a second runtime when one is already active, and it
never retries a failed allocation automatically.

### First-time authentication

`setup.sh` runs `colab usage` when stdin is a terminal. The Colab CLI prints a
Google sign-in URL and prompts for the authorization code; complete it once and
the refresh token is stored in the OS keyring. If the keyring is unavailable,
the CLI asks again on the next command. No Google token is written into this
repository.

If setup was run without a TTY, authenticate explicitly before `up.sh`:

```bash
colab usage
```

## Daily use

```bash
./scripts/colab/connect.sh
cd ~/workspace
fastfetch
codex --version
claude --version
agy --version
opencode --version
gh --version
tmux new -s agents
```

The login user is `root` because that is the only SSH identity Colab exposes.
`/home/agent` is a compatibility symlink to `/root` (or a real directory when
the image provides one), so the familiar `/home/agent/workspace` path works.
The shell starts in `~/workspace`.

Run one-shot commands without an interactive shell:

```bash
./scripts/colab/connect.sh -- bash -lc 'cd ~/workspace && git status'
./scripts/colab/connect.sh -- tmux new -As agents
```

### Agent authentication

Authenticate each agent interactively inside the runtime:

```bash
codex login --device-auth
claude
agy
opencode auth login
gh auth login
```

The runtime is less trusted than your own machine. Local Codex/Claude/OpenCode
credentials and OAuth tokens are **not** copied to Colab. Interactive login is
the supported path. This is deliberate: copying local tokens would give a
hosted VM long-lived credentials with no additional isolation.

For GitHub HTTPS workflows, either run `gh auth login` inside the runtime or
send a short-lived fine-grained PAT over an encrypted SSH stdin pipe:

```bash
./scripts/colab/set-gh-token.sh
```

The PAT is never stored locally by that script. It is written only to the
disposable runtime's `gh` configuration and disappears with the runtime. Use
`gh auth setup-git` (called by the script) so Git uses the credential helper.

### Saving work

Colab runtimes are disposable and can be reclaimed at any time. Use Git as the
primary persistence mechanism:

```bash
cd ~/workspace
git status
git add -A
git commit -m "wip"
git push
```

For uncommitted work, caches, or generated artifacts, archive the complete
workspace to your local machine:

```bash
./scripts/colab/save.sh
# -> backups/colab-workspace-YYYYmmddTHHMMSSZ.tar.gz (mode 0600)
```

Restore an archive into a fresh runtime:

```bash
./scripts/colab/restore.sh backups/colab-workspace-....tar.gz
# extracts to /home/agent/workspace.restore-<timestamp>

./scripts/colab/restore.sh --force backups/colab-workspace-....tar.gz
# merges into the live workspace, overwriting matching files
```

The local archive may contain secrets from the workspace, so it is created with
mode `0600`; do not commit `backups/` (it is ignored). Google Drive is not
required. If you want it, the Colab CLI provides `colab drivemount`; it is
optional and not part of the default workflow.

## Persistence matrix

| State | Runtime active | After `down.sh` or expiration |
|---|---|---|
| `/home/agent/workspace` | Preserved | Destroyed |
| Git remotes | Preserved by `git push` | Preserved |
| Local `save.sh` archives | Preserved | Preserved |
| Agent OAuth/login sessions | Preserved | Destroyed |
| GitHub PAT sent with `set-gh-token.sh` | Preserved | Destroyed |
| `.colab/` SSH key, config, Git identity | Local only | Preserved |
| Codex/Claude/OpenCode auth from the host | Never copied | Never copied |
| Docker images/volumes | Not available | Not applicable |

The local `.colab/` directory contains a dedicated Ed25519 key, the session
config, the Git identity file, and the generated SSH alias. It is ignored by
Git. It contains no Google OAuth token and no GitHub PAT.

## Feature parity

| Capability | Docker backend | Colab backend |
|---|---|---|
| Codex CLI | Yes | Yes, installed by bootstrap |
| Claude Code | Yes | Yes, installed by bootstrap |
| Antigravity CLI (`agy`) | Yes | Yes, official installer (best effort) |
| OpenCode | Yes | Yes, installed by bootstrap |
| Git + GitHub CLI | Yes | Yes, interactive `gh` login or PAT |
| Neovim + shared config | Yes | Yes, current Neovim from upstream |
| Zsh + Oh My Zsh + Starship | Yes | Yes |
| tmux | Yes | Yes |
| Python, Node.js, C/C++, CMake | Yes | Yes, Node.js 22 LTS from nodejs.org |
| `/home/agent/workspace` | Yes | Yes, compatibility path |
| Persistent credentials across recreation | Yes | No, by design |
| Docker-in-Docker | Yes, isolated sidecar | No, unsupported |
| Tailscale service identity | Host-exposed port | No, Colab WebSocket SSH |
| Inbound ports / privileged containers | Controlled | Not available |
| VM-grade isolation | No (shared kernel) | Yes, managed Google VM |
| Unprivileged `agent` user | Yes (UID 10001) | No, Colab login is root |
| systemd | No | No |

### Docker-in-Docker

The Colab runtime exposes `/dev/fuse`, but it does not provide a supported
privileged-container contract. Running a second Docker daemon would require
either privileged capabilities or a rootless setup that is not guaranteed by
Colab, and it would add a large attack surface to a hosted VM. The Colab
bootstrap therefore does not install Docker. Use the Docker backend when
container workflows are required.

## Lifecycle and recovery

`up.sh` lifecycle:

1. Validates options and the local configuration.
2. Runs a read-only `colab usage` check (also verifies authentication).
3. Lists server assignments and compares them with the tracked session and
   recorded endpoint.
4. Reuses the runtime if it exists; otherwise creates exactly one runtime.
5. Waits for SSH and compares the remote bootstrap hash with the local content
   hash.
6. Uploads and runs the bootstrap only when needed (or with `--rebuild`).
7. Writes a completion marker only at the end.

Safe-failure behavior:

- If authentication, session listing, or the API fails, nothing is created.
- If a recorded endpoint is still active but its local session entry is gone,
  `up.sh` refuses to create another runtime and asks you to resolve it with
  `colab sessions`.
- If allocation fails, it is not retried.
- If bootstrap is interrupted, the runtime is left running with no marker; a
  later `up.sh` resumes the bootstrap without creating a new runtime.
- If SSH never becomes reachable, the runtime is left running and the error
  explains how to resume or release it.
- `down.sh` stops only the tracked session. It never uses `colab stop --all`.

Runtime expiration: Colab can reclaim a runtime after inactivity or at its
maximum lifetime. `status.sh` and `up.sh` detect a missing assignment, but any
unsaved workspace data is gone. Push or `save.sh` frequently.

SSH session limit: Colab allows a small number of concurrent SSH sessions. A
stale client can cause HTTP 429. `connect.sh --force` kills stale sessions on
this runtime before connecting. Use it only when you are sure no other client
is active.

## Monitoring compute units

```bash
./scripts/colab/status.sh
colab usage
```

`status.sh` prints the account balance, current usage rate, active assignments,
remote uptime, CPU/memory/disk, workspace size, bootstrap freshness, and tool
versions. The Colab CLI itself performs no background polling; the Agent
Sandbox scripts make only the API calls needed for the command you ran.

CPU runtimes are the default because accelerators consume compute units much
faster. Request a GPU or TPU only when the workload needs it, and release the
runtime with `down.sh` when finished.

## Security model

- The hosted runtime is less trusted than your machine. Treat it as a
  disposable, internet-connected VM.
- Local OAuth tokens and GitHub PATs are not copied automatically. `set-gh-token.sh`
  sends a PAT only when explicitly invoked, over SSH stdin, and stores it only on
  the runtime.
- The generated SSH key is dedicated to Colab and lives in `.colab/` with mode
  `0600`. It is not your personal `~/.ssh/id_ed25519` key.
- Inner SSH host keys are not verified because Colab presents an ephemeral
  managed endpoint. The outer WebSocket connection is TLS-authenticated by
  Google; the generated SSH config disables known-hosts pinning for the inner
  transport. Do not reuse this config for other hosts.
- The runtime login is `root`. Colab does not expose an unprivileged login, so
  the Docker backend's UID 10001 boundary cannot be reproduced. Any process you
  run can read the runtime's credentials and workspace.
- Public internet egress is available. There is no domain allowlist. Do not run
  untrusted workloads on a machine holding valuable credentials.
- The Colab API/session tokens live in the Colab CLI's OS keyring entry on your
  client, not in this repository. Revoke access from your Google account if a
  client is compromised.
- `save.sh` archives may contain secrets. They are mode `0600` and ignored by
  Git; handle them like any other credential-bearing backup.

## Configuration

`setup.sh` writes `.colab/config.env` (mode `0600`, ignored by Git). Variables
can be overridden by exporting them before a command:

| Variable | Default | Purpose |
|---|---|---|
| `COLAB_SESSION` | `agent-sandbox` | Session name tracked locally |
| `COLAB_AUTH` | `oauth2` | Colab CLI auth provider (`oauth2` or `adc`) |
| `COLAB_STATE_FILE` | empty | Optional `colab --config` path |
| `COLAB_CLI` | detected | Absolute `colab` executable path |
| `COLAB_SSH_KEY` | `.colab/ssh/id_ed25519` | Dedicated client key |
| `COLAB_WORKSPACE` | `/home/agent/workspace` | Remote workspace path |
| `COLAB_CODEX_VERSION` | `latest` | npm version for Codex |
| `COLAB_CLAUDE_VERSION` | `latest` | npm version for Claude Code |
| `COLAB_OPENCODE_VERSION` | `latest` | npm version for OpenCode |
| `COLAB_AGY_INSTALL` | `1` | Install Antigravity CLI |
| `COLAB_SSH_WAIT_ATTEMPTS` | `30` | SSH readiness attempts |
| `COLAB_SSH_WAIT_SECONDS` | `2` | Delay between attempts |

`setup.sh` copies `CODEX_VERSION`, `CLAUDE_VERSION`, and `OPENCODE_VERSION` from
the Docker `.env` when those values are present, so the two backends can pin the
same releases without a second configuration ecosystem.

## Troubleshooting

### `colab usage` fails or asks for a code repeatedly

Complete the sign-in flow in a terminal. If the OS keyring is locked, unlock it
or use `colab --auth adc usage` with Application Default Credentials. A
non-interactive shell cannot complete the OAuth prompt.

### `up.sh` says a runtime is still active but the local entry is gone

Run `colab sessions` and identify the assignment. Do not create another runtime
until it is stopped. If the local session store was lost, stop the assignment
explicitly from a shell where the Colab CLI can see it; the Agent Sandbox
scripts intentionally never guess or stop unknown sessions.

### SSH returns 404

The runtime predates SSH support or was created without it. Release it with
`./scripts/colab/down.sh` and create a fresh one with `./scripts/colab/up.sh`.

### SSH returns 429 or `already-active-session`

The runtime's concurrent SSH limit is exhausted, often by a dead client. Close
the other client, or run `./scripts/colab/connect.sh --force`.

### `up.sh` stops during bootstrap

Re-run `./scripts/colab/up.sh`. It reuses the runtime, detects the missing
completion marker, and resumes the bootstrap. Inspect the remote log with:

```bash
./scripts/colab/connect.sh -- tail -n 100 /root/.agent-sandbox/bootstrap.log
```

If the runtime is unusable, run `./scripts/colab/down.sh` and start again.

### A tool is missing or an agent fails to start

Run `./scripts/colab/status.sh` to see the versions installed by the bootstrap.
`./scripts/colab/up.sh --rebuild` re-runs the bootstrap and reinstalls missing
npm tools. Antigravity and Fastfetch are best-effort; their upstream installers
can fail without affecting the core sandbox.

### Git pushes fail

Inside the runtime, run `gh auth status`. If unauthenticated, run `gh auth login`
or `./scripts/colab/set-gh-token.sh` from the client. The runtime has its own Git
identity from `.colab/gitconfig`; check it with `git config --global --list`.

### Prompt icons look wrong

Install a Nerd Font in your terminal emulator. This is a client-side setting;
the runtime cannot fix it.

## Manual verification

The automated suite uses fake `colab` and `ssh` commands and does not need a
Google account:

```bash
python3 -m unittest discover -s tests -v
```

A real CPU-runtime smoke test (requires authenticated Colab access and consumes
a small amount of compute units) is:

```bash
./scripts/colab/setup.sh
./scripts/colab/up.sh
./scripts/colab/connect.sh -- 'set -e
  cd ~/workspace
  command -v zsh tmux nvim starship eza gh codex claude opencode node python3 cmake
  codex --version
  claude --version
  opencode --version
  gh --version
  nvim --version | head -n 1
  git config --global user.name
  test -d ~/workspace'
./scripts/colab/status.sh
./scripts/colab/save.sh /tmp/colab-smoke.tar.gz
./scripts/colab/down.sh --yes
colab sessions
colab usage
```

Verified in this repository's development environment:

- Local CLI/config validation and command construction with fake commands.
- Provisioning exactly one runtime, idempotent reconnects, and bootstrap resume.
- Refusal to create a runtime when lookup/auth fails or an endpoint is orphaned.
- Explicit release of only the tracked session.
- Secret handling: the PAT is sent over stdin and never appears in argv.
- Workspace archive creation and restore command construction.
- Docker backend static contract tests remain green.
- A live CPU-runtime smoke test on 2026-11-20 provisioned a standard runtime,
  installed every required tool (Codex 0.171.0, Claude Code 2.1.329, OpenCode
  1.19.6, Antigravity 1.5.5, GitHub CLI 2.103.2, Neovim 0.12.6, Node 22.23.5,
  Zsh 5.9, tmux 3.4, Starship 1.27.0, eza, Fastfetch, Python 3.13, CMake),
  reconnected without reinstalling, verified `/home/agent/workspace` and the
  Git identity, saved and restored a workspace archive, and released the
  runtime. `colab sessions` was empty and the usage rate returned to 0.00/hr
  afterward.
- The live run exposed two installer defects that are now fixed: the Node
  tarball checksum looked for the wrong filename, and npm's global prefix
  pointed inside the versioned Node directory. Static contract tests cover both
  fixes. The interrupted-bootstrap recovery path was exercised live: a failed
  bootstrap left the runtime active, and the next `up.sh` resumed installation
  without creating another runtime.

The live smoke test requires authenticated Colab access and consumes a small
amount of compute units (the CPU runtime observed 0.08 CU/hour while active).
Do not initiate interactive authentication just to run it; use the fake-command
suite when no account is available.

## Known limitations

- No Docker-in-Docker and no privileged containers.
- No unprivileged `agent` user; the runtime login is `root`.
- No persistent remote filesystem. Git and `save.sh` are the persistence story.
- No Tailscale identity for the runtime; access is through the Colab WebSocket
  proxy.
- Colab may reclaim the runtime at any time, including during a long build.
- Antigravity and Fastfetch installation are best-effort because they depend on
  third-party installers/assets.
- Colab CLI output and API behavior are upstream contracts; pin the CLI version
  in controlled environments if exact behavior matters.
- The inner SSH host key is ephemeral and is not pinned.

## References

- [Google Colab CLI](https://github.com/googlecolab/google-colab-cli)
- [Colab CLI session management design](https://github.com/googlecolab/google-colab-cli/blob/main/docs/01_session_management.md)
- [Colab terms of service](https://research.google.com/colaboratory/tos_v6.html)
- [Colab compute units](https://colab.research.google.com/signup)
