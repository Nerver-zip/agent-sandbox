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
  │                                      └─ bootstrap over SSH
  │                                           ├─ parallel apt/Node/npm/nvim tasks
  │                                           ├─ validation
  │                                           └─ optional cache capture
  │
  ├── .colab/cache/  <── validated gzip artifacts over SSH (small, reusable data)
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
- The runtime is bootstrapped over SSH. The bootstrap is idempotent and records
  a content hash; `up.sh` skips it when nothing changed.
- Independent installation tasks run concurrently. The critical path is the
  slowest package source instead of the sum of every download: apt, Node.js,
  Neovim, Starship, Oh My Zsh, and Antigravity start together; the three agent
  CLIs install into separate prefixes in parallel; Neovim plugin setup overlaps
  the agent installs.
- A selective, opt-in environment cache stores small reusable artifacts
  (Neovim plugin data and optional shell tooling). It is off by default because
  the measured restore was slower than the parallel bootstrap on a normal
  connection; it remains available for slow or restricted networks. Large
  Node/npm trees are intentionally not cached. See
  [Environment cache](#environment-cache).
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

# Inspect, prune, invalidate, or clear local cache artifacts.
./scripts/colab/cache.sh
./scripts/colab/cache.sh list
./scripts/colab/cache.sh invalidate nvim-plugins

# Archive the complete remote workspace locally.
./scripts/colab/save.sh

# Release the runtime (prompts unless --yes is passed).
./scripts/colab/down.sh
```

`up.sh --cache` enables the optional environment cache; `--no-cache` overrides a
configured cache for one run. A fresh runtime captures missing components after
its validated bootstrap. To populate the cache from an already running runtime
without reinstalling, use `--refresh-cache`. The cache is off by default because
the measured cold restore was slower than the parallel bootstrap on a normal
connection. It remains useful on slow or restricted GitHub/proxy networks and for
reproducible Neovim plugin snapshots. Useful cache and recovery flags:

```bash
./scripts/colab/up.sh --cache                   # enable restore/capture
./scripts/colab/up.sh --no-cache                # bypass restore and capture
./scripts/colab/up.sh --refresh-cache           # recapture from the active runtime
./scripts/colab/up.sh --restore-workspace       # newest local workspace archive
./scripts/colab/up.sh --restore-workspace=backups/colab-workspace-....tar.gz
./scripts/colab/up.sh --benchmark               # print phase/network measurements
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

./scripts/colab/restore.sh --replace-empty backups/colab-workspace-....tar.gz
# extracts into the live workspace only when it is empty

./scripts/colab/restore.sh --force backups/colab-workspace-....tar.gz
# merges into the live workspace, overwriting matching files
```

After a fresh `up.sh`, restore the newest archive automatically. The live
workspace is replaced only when it is empty; a populated workspace is never
overwritten:

```bash
./scripts/colab/up.sh --restore-workspace
```

Workspace archives and environment-cache artifacts are separate. `save.sh`
captures project files and may contain secrets; `cache.sh` captures only the
tool paths listed in `scripts/colab/cache-remote.sh`.

The local archive may contain secrets from the workspace, so it is created with
mode `0600`; do not commit `backups/` (it is ignored). Google Drive is not
required. If you want it, the Colab CLI provides `colab drivemount`; it is
optional and not part of the default workflow.

## Environment cache

The cache exists to reduce repeated work, not to move the whole runtime. It is
opt-in and deliberately selective because the Colab SSH proxy is slower than
Colab's direct package mirrors.

### What is cached

| Component | Contents | Approx. compressed size | Status |
|---|---|---|---|
| `nvim-plugins` | Lazy.nvim plugins, Treesitter parsers, Neovim state/cache | 15-20 MiB | opt-in |
| `shell` | `/opt/oh-my-zsh` and the Starship binary | 5-15 MiB | opt-in |

`nvim-plugins` is the recommended component because it removes the most CPU and
network work. `shell` is available for slow/restricted networks. Both are
enabled with `COLAB_CACHE=1` (or `up.sh --cache`); the default is off because the
measurements below showed the parallel bootstrap is faster on a normal
connection.

These are the components with expensive enough standalone reconstruction to
consider caching; under the concurrent bootstrap the critical path is apt/npm,
so the cache remains opt-in. `codex`, `claude`, `opencode`, Node.js, and apt
packages are **not** cached as installed trees: the 1.2 GiB npm tree compresses
to roughly 524 MiB, which takes longer to upload than Colab takes to download
and install the same tools. Rebuilding them in parallel is faster and smaller.
The component framework is generic; `scripts/colab/cache-remote.sh` is the
single place that defines capture paths, and `cache.sh invalidate` drops one
component without invalidating the others.

The cache never contains:

- `/home/agent/workspace` or any project file
- agent configuration or credentials (`~/.codex`, `~/.claude`, `~/.config/opencode`, `~/.gemini`)
- SSH keys or `~/.ssh`
- GitHub tokens, shell history, or npm credentials

### Lifecycle

1. A new runtime runs the normal bootstrap.
2. If the bootstrap succeeds and passes validation, missing cache components are
   captured to `.colab/cache/v1/<profile>/<component>/` with a manifest and
   SHA-256 checksum.
3. A later fresh runtime (no bootstrap marker) detects the cache profile, checks
   the manifest and checksum locally, restores only compatible artifacts, and
   marks them on the runtime.
4. The normal bootstrap still runs. It skips any component that is already
   current, installs everything missing, applies the shared configuration, and
   validates the result. Cache restoration never replaces this validation.
5. If a restored component is corrupt, incompatible, or causes the bootstrap to
   fail, the artifact is discarded and the next run installs normally.

An existing runtime with a current marker does no cache work. A stale marker
(configuration change) does not restore cache paths over an existing
environment; it only reruns the required bootstrap steps. `--rebuild` forces a
refresh, and `--refresh-cache` recaptures from the active runtime without
reinstalling.

### Compatibility and integrity

Every artifact records:

- cache format version (`v1`)
- runtime profile: OS ID, version, architecture, and glibc version
- component and cache ID
- exact Neovim version and payload `lazy-lock.json` hash for `nvim-plugins`
  (the actual remote lockfile is recorded for diagnostics)
- file size, SHA-256, and creation timestamp

Restore requires an exact profile match, a matching payload lockfile for Neovim plugins,
a valid manifest, matching size and SHA-256, and a successful remote extraction.
The remote helper validates every archive member against the component allowlist
before extraction, rejecting absolute paths, `..`, and files outside the tool
paths. A missing, truncated, corrupt, or incompatible artifact always falls back
to the normal bootstrap. The bootstrap's final validation must still pass before
`up.sh` reports success.

### Storage and cleanup

The cache lives under `.colab/cache/` (ignored by Git, mode `0700`; artifacts are
mode `0600`). Defaults:

- keep the newest 2 artifacts per component and profile
- total budget 1024 MiB
- remove interrupted captures and orphaned artifacts during prune

```bash
./scripts/colab/cache.sh status
./scripts/colab/cache.sh list
./scripts/colab/cache.sh prune
./scripts/colab/cache.sh invalidate nvim-plugins
./scripts/colab/cache.sh clear
```

The cache is local-only and needs no Google Drive, cloud bucket, or account
beyond the Colab CLI. Google Drive was considered as an optional fast backend,
but it would add mount/authentication state and is not needed for the small
artifacts the cache is designed to hold.

### Measured results

All measurements use the same standard CPU runtime shape and separate runtime
allocation/SSH readiness from environment preparation. Times are wall-clock
seconds from `up.sh`; network volume is from the remote interface counters with
`--benchmark` (includes protocol overhead). See the live verification section
for the exact dates and versions.

| Scenario | Allocation + SSH | Cache restore | Bootstrap | Cache capture | Total |
|---|---|---|---|---|---|
| Original sequential bootstrap (before this work) | 35 | n/a | 148 | 0 | 183 |
| New parallel bootstrap, no cache (default) | 12 | 0 | 98 | 0 | **125** |
| Optional cache, cold restore (`--cache`) | 14 | 15 | 97 | 0 | 144 |
| Existing runtime reconnect | 8 | 0 | 0 | 0 | 15 |
| Partial cache invalidation (nvim artifact missing) | 13 | 0 | 68 | 12 | 107 |

The parallel bootstrap is the primary improvement: **148 s → about 95 s** of
environment preparation and **183 s → about 125 s** total on a standard CPU runtime.
The optional Neovim cache makes the cold path slower on this connection because
its 15 s transfer overlaps work that the parallel bootstrap already hides. It is
kept for slow or restricted networks and is never enabled by default.

Large-artifact experiments rejected by the measurements:

| Artifact | Compressed size | Restore over SSH | Normal install |
|---|---|---|---|
| npm agent tree | ~524 MiB | ~150 s upload + 16 s extract | ~40-60 s in parallel |
| Node.js installed tree | ~63 MiB | ~9-20 s | ~8-12 s download + extract |
| apt `.deb` set | ~13.5 MiB | ~5 s + ~70 s dpkg | ~77-81 s including dpkg |
| Neovim plugin data | ~18 MiB | ~10-15 s | ~15 s standalone, ~3 s on the concurrent critical path |
| Oh My Zsh + Starship | ~12.7 MiB | ~9 s | ~2-3 s |

The proxy transported a single stream at roughly 1.4-3.5 MB/s upload and 3-10
MB/s download. Three parallel CLI uploads reached ~7.3 MB/s aggregate, still
below Colab's 15-20 MB/s apt mirror. This is why neither a full environment
archive nor the npm/Node trees became the default.

## Persistence matrix

| State | Runtime active | After `down.sh` or expiration |
|---|---|---|
| `/home/agent/workspace` | Preserved | Destroyed |
| Git remotes | Preserved by `git push` | Preserved |
| Local `save.sh` archives | Preserved | Preserved |
| Local environment cache (`.colab/cache/`) | Preserved | Preserved |
| Agent OAuth/login sessions | Preserved | Destroyed |
| GitHub PAT sent with `set-gh-token.sh` | Preserved | Destroyed |
| `.colab/` SSH key, config, Git identity | Local only | Preserved |
| Codex/Claude/OpenCode auth from the host | Never copied | Never copied |
| Docker images/volumes | Not available | Not applicable |

The local `.colab/` directory contains a dedicated Ed25519 key, the session
config, the Git identity file, and the generated SSH alias. It is ignored by
Git. It contains no Google OAuth token and no GitHub PAT. The environment cache
contains only the tool paths listed above; it is separate from workspace
archives and from agent credentials.

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
6. When the optional cache is enabled on a fresh runtime, restores compatible
   local cache artifacts after checking their profile, manifest, size, and
   SHA-256.
7. Uploads and runs the bootstrap only when needed (or with `--rebuild`). The
   bootstrap runs independent installs concurrently, installs anything the cache
   did not supply, applies configuration, and validates every required tool.
8. Captures missing cache components after a successful, validated bootstrap
   when the cache is enabled.
9. Writes a completion marker only at the end.

Safe-failure behavior:

- If authentication, session listing, or the API fails, nothing is created.
- If a recorded endpoint is still active but its local session entry is gone,
  `up.sh` refuses to create another runtime and asks you to resolve it with
  `colab sessions`.
- If allocation fails, it is not retried.
- If bootstrap is interrupted, the runtime is left running with no marker; a
  later `up.sh` resumes the bootstrap without creating a new runtime.
- A cache artifact that fails integrity, extraction, or compatibility checks is
  discarded; the bootstrap installs the component normally.
- If the bootstrap fails after a cache restore, the restored artifacts are
  invalidated so a bad cache is not retried.
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
- Environment-cache artifacts are mode `0600` under `.colab/cache/` and contain
  only explicitly allowlisted tool paths. The remote helper rejects archives
  with absolute paths, `..`, or entries outside the component allowlist before
  extraction. Credentials, agent config, SSH keys, shell history, and the
  workspace are never captured.

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
| `COLAB_CACHE` | `0` | Enable local environment cache restore/capture |
| `COLAB_CACHE_COMPONENTS` | `nvim-plugins` | Space-separated components (`nvim-plugins`, `shell`) |
| `COLAB_CACHE_KEEP` | `2` | Newest artifacts kept per component/profile |
| `COLAB_CACHE_MAX_MB` | `1024` | Total local cache size budget in MiB |

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

### The cache seems stale or a restored component is broken

Cache artifacts are validated on every restore and automatically discarded on
mismatch, corruption, extraction failure, or bootstrap failure. To force a
complete rebuild:

```bash
./scripts/colab/cache.sh clear
./scripts/colab/up.sh --rebuild
```

To drop only one component:

```bash
./scripts/colab/cache.sh invalidate nvim-plugins
```

Cache maintenance never stops or modifies a runtime by itself. `up.sh` falls
back to the normal bootstrap whenever a cache cannot be used.

### Workspace restore refuses to replace files

`--replace-empty` intentionally refuses a populated workspace. Use the default
restore directory or `--force` after checking the archive contents:

```bash
./scripts/colab/restore.sh backups/colab-workspace-....tar.gz
./scripts/colab/up.sh --restore-workspace
```

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
./scripts/colab/up.sh --benchmark
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
./scripts/colab/cache.sh list
./scripts/colab/save.sh /tmp/colab-smoke.tar.gz
./scripts/colab/down.sh --yes
./scripts/colab/up.sh --cache --benchmark    # optional cached cold restore
./scripts/colab/cache.sh status
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
- Cache lifecycle tests cover capture, restore, profile mismatch, checksum
  corruption, partial invalidation, restore failure, capture failure, credential
  exclusion, archive path rejection, pruning, size limits, and fallback.
- A live CPU-runtime baseline on 2026-11-20 used the original sequential
  bootstrap: **183 s total** (35 s allocation/SSH, 148 s bootstrap).
- The parallel no-cache bootstrap measured **120-125 s total** (12-14 s
  allocation/SSH, 94-98 s bootstrap, 629.7 MiB remote receive) on the same CPU
  shape. All required
  tools validated: Codex 0.171.0, Claude Code 2.1.329, OpenCode 1.19.6,
  Antigravity 1.5.5, GitHub CLI 2.103.2, Neovim 0.12.6, Node 22.23.5,
  Zsh 5.9, tmux 3.4, Starship 1.27.0, eza, Fastfetch, Python 3.13, CMake.
- The optional Neovim cache measured **18.0 MiB**, captured in 5-7 s and restored
  in 10-15 s. A cold cached run totaled **144 s** (14 s allocation/SSH, 15 s
  restore, 97 s bootstrap), slower than the 120 s no-cache run, which is why the
  cache is opt-in. An earlier two-component cache (`nvim-plugins` + `shell`)
  measured 30.7 MiB and 22 s restore.
- An existing-runtime reconnect measured 15 s total with no bootstrap or cache
  work. A missing component fell back to the normal bootstrap and was not
  reported as a cache success.
- The live runs exercised capture, cold restore, cache refresh, invalidation,
  a missing-component fallback, and reconnect. Cache artifacts were verified by
  size/SHA-256 before upload and by the normal bootstrap validation after
  extraction. The remote lockfile difference between the payload lock and
  Lazy's installed commit is recorded for diagnostics and does not invalidate
  an otherwise working plugin snapshot.
- The baseline run also exposed two installer defects that were already fixed:
  the Node tarball checksum looked for the wrong filename, and npm's global
  prefix pointed inside the versioned Node directory. Static contract tests cover
  both fixes.

The live smoke test requires authenticated Colab access and consumes a small
amount of compute units (the CPU runtime observed 0.08 CU/hour while active).
Do not initiate interactive authentication just to run it; use the fake-command
suite when no account is available.

## Known limitations

- No Docker-in-Docker and no privileged containers.
- No unprivileged `agent` user; the runtime login is `root`.
- No persistent remote filesystem. Git, `save.sh`, and the small local tool
  cache are the persistence story.
- No Tailscale identity for the runtime; access is through the Colab WebSocket
  proxy.
- Colab may reclaim the runtime at any time, including during a long build.
- Antigravity and Fastfetch installation are best-effort because they depend on
  third-party installers/assets.
- The environment cache is opt-in and selective. It currently covers Neovim
  plugin data, with an optional `shell` component. Node.js, apt packages, and the
  agent CLI trees are reinstalled rather than transferred because the SSH proxy
  is slower than the upstream package sources. `COLAB_CACHE_MAX_MB` and
  `cache.sh prune` bound the cache, but a new runtime still needs network access
  to npm, GitHub, and the apt mirrors.
- Cache artifacts are versioned by OS, architecture, glibc, component version,
  and the payload lockfile. Upstream Neovim or lockfile changes invalidate the
  plugin artifact; run `up.sh --cache --rebuild` to reinstall and recapture.
- Parallel installs share the runtime's two vCPUs and network. Individual apt and
  npm times varied by roughly 20-30% between live runs; the reported total is a
  representative single run, not a statistical median.
- Colab CLI output and API behavior are upstream contracts; pin the CLI version
  in controlled environments if exact behavior matters.
- The inner SSH host key is ephemeral and is not pinned.

## References

- [Google Colab CLI](https://github.com/googlecolab/google-colab-cli)
- [Colab CLI session management design](https://github.com/googlecolab/google-colab-cli/blob/main/docs/01_session_management.md)
- [Colab terms of service](https://research.google.com/colaboratory/tos_v6.html)
- [Colab compute units](https://colab.research.google.com/signup)
