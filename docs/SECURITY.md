# Security model and operational boundaries

## Goals

The unprivileged `agent` workspace should not receive the host's SSH private key, host Docker socket, homelab bind mounts, production files, root login or a privileged container. A dedicated Docker bridge and host iptables `DOCKER-USER`/`INPUT` rules block **new connections** from this bridge to other RFC1918 networks, CGNAT/tailnet (`100.64/10`), loopback, link-local, host services, and other Docker bridges. SSH exposure is restricted to the host's Tailscale IPv4 address. CPU, RAM, and process limits reduce accidental resource exhaustion. Docker capability inside the workspace is provided via an isolated Docker-in-Docker (DinD) sidecar service without sharing or exposing the host Docker daemon.

## Docker-in-Docker (DinD) isolation

To provide Docker capabilities to coding agents without exposing the host's Docker daemon (`/var/run/docker.sock`) or compromising other homelab containers:

1. **No Host Docker Socket Mount**: The workspace container has NO access to `/var/run/docker.sock`. It cannot see, inspect, modify, stop, or exec into any host or homelab containers.
2. **Dedicated Daemon**: The workspace container communicates with the sidecar daemon over TCP (`DOCKER_HOST=tcp://dind:2375`) on the isolated `agent_sandbox_net` bridge.
3. **Privilege Boundary**: While the `dind` container runs with required container-in-container capabilities (`privileged: true`), it does not mount any host directories or the host socket. The `workspace` container itself remains strictly unprivileged (`privileged: false`, `cap_drop: [ALL]`, `no-new-privileges: true`, non-root UID 10001).
4. **Network Firewall Enforcement**: Any containers spawned inside the DinD daemon share the `agent_sandbox_net` bridge egress path. The host iptables rules (`ASBX_EGRESS`) apply equally to DinD egress: inner containers can access the public Internet (e.g. `docker pull`), but are blocked from reaching the host, LAN, tailnet, and other Docker bridges.
5. **Local Socket Proxy**: For compatibility with tools, test runners, and agent harnesses that query `unix:///var/run/docker.sock` directly, the workspace container runs an internal `socat` bridge forwarding `/var/run/docker.sock` to `tcp://dind:2375`. This socket is created inside the container and does not mount or expose the host's Docker daemon.

## Explicit exceptions

1. SSH host keypair is mounted read-only into the container for SSHD to read as root. The `agent` user cannot directly read the private key by ordinary Unix permissions.
2. `persistent/codex` is mounted read-write at `/home/agent/.codex-auth` as user UID 10001, with directory mode `0700` and credential file mode `0600`. Only the Codex auth document, its shared lock, and private atomic-write temporaries are allowed in this directory. Anyone executing as `agent` can read the auth file and can potentially send it to public Internet services. There is **no safe way** to promise both unrestricted agent commands and secrets that the agent cannot access while its own process needs them.
3. `persistent/claude/.credentials.json` is mounted read-write for Claude Code authentication. The `agent` account and its processes can read and transmit it. OpenCode and Antigravity credentials remain in the disposable workspace instead.
4. `persistent/gh/token` is a fine-grained PAT mounted read-only, owned by UID 10001. The agent can read and exfiltrate it, even if the wrapper keeps it out of the global login shell environment. Github PAT scopes and expiration must be tightly restricted. The official CLI (`/usr/bin/gh`) is not a safety boundary: agents can run it, the wrapper, or directly read the token file.
5. Public HTTPS egress is allowed so agents can download packages, clone public Git repos and contact external AI APIs. That also allows data exfiltration and public-network scanning. The firewall is not a general domain allowlist, proxy or content filter.
6. The embedded Docker DNS resolver may provide DNS responses. Docker's DNS proxy can have configuration-dependent behavior; do not regard DNS isolation as preventing every form of internal name or metadata discovery.
7. `persistent/ssh/authorized_keys` (client *public* keys) and `persistent/ssh/known_hosts` are mounted read-write as user UID 10001 so authorized devices (desktop, laptop, phones) and outgoing SSH host verifications survive container recreation. They do not contain private keys.

## Optional Token Monitor integration

The host reads only allowlisted accounting sources and runs the upstream collector in a separate non-root container with its network disabled. A private Unix socket allows the collector to submit usage to the configured Hub through the host. The Hub secret stays outside both containers. The firewall, sandbox bridge and workspace mounts are unchanged. Selected transcripts remain private host data during staging and recovery. By default the Hub receives project folder labels and path-derived identifiers. Repository content, absolute paths and session titles are excluded from the integration's project reporting. [Token Monitor trust boundary and retention](TOKEN_MONITOR.md#trust-boundary)

## Google Colab backend trust boundary

The optional Colab backend is a separate trust zone from the Docker deployment. It runs on a managed Google VM reached through the official Colab CLI's WebSocket SSH proxy. It does not use the host Docker daemon, the Docker bridge, the host firewall rules, or the Docker credential mounts described above.

- The runtime login is `root` because that is the only SSH identity Colab exposes. The unprivileged UID 10001 boundary of the Docker backend cannot be reproduced. Any process running in the runtime can read the workspace and every credential stored on that runtime.
- A dedicated Ed25519 client key is generated under `.colab/ssh/` with mode `0600`. It is not the user's personal SSH key and is ignored by Git.
- Local OAuth tokens, `persistent/codex`, `persistent/claude`, and `persistent/gh` are never copied to the runtime. Codex, Claude Code, Antigravity, OpenCode, and GitHub CLI authenticate interactively inside the runtime.
- `set-gh-token.sh` is an explicit opt-in path for a fine-grained GitHub PAT. It sends the token over SSH stdin, never through argv, and stores it only on the disposable runtime. The token disappears with the runtime; revoke it in GitHub if the runtime is compromised.
- The outer transport is a TLS-authenticated WebSocket to Google. The inner SSH host key is ephemeral and is deliberately not pinned; the generated config disables known-hosts verification for that one alias. Do not reuse the generated config for other hosts.
- The runtime has public internet egress. There is no domain allowlist, traffic inspection, or network isolation comparable to the Docker firewall. Treat the runtime as less trusted than the local machine and avoid placing long-lived high-value secrets in it.
- The remote workspace is ephemeral. Git pushes and `save.sh` archives are the persistence mechanisms. Local archives can contain secrets and are created with mode `0600`; `backups/` is ignored by Git.
- Docker-in-Docker is not supported. A second Docker daemon would require privileged capabilities or a rootless configuration that Colab does not guarantee, and would increase the hosted VM's attack surface.
- Colab may reclaim the runtime after inactivity or at its maximum lifetime. The scripts do not add artificial activity or keepalive mechanisms; `save.sh` and Git are the recovery path.

## Limits and known risks

- Docker shares the host's Linux kernel; a container escape vulnerability could break isolation. For genuinely untrusted code, use a VM/microVM with separate Tailscale identity and egress controls, ideally on a separate host or VLAN.
- Root inside a Docker container is not equivalent to root on the host, but the SSHD process must start as root and retain a few capabilities; it then drops to the unprivileged `agent` account on login.
- Persistent mounts are explicitly allowlisted in Compose. The Codex credential directory is a narrow directory mount so atomic replacement works; the complete host `.codex` directory is never mounted. Do not add mounts for convenience without reviewing the trust boundary.
- The firewall assumes Docker iptables mode with `DOCKER-USER`. It does **not** support native Docker nftables mode, rootless Docker or remote contexts. It is stateful host networking logic, so always start via `scripts/up.sh`. Host firewall rules are not automatically re-applied after every independent Docker daemon/firewall reload. Deploy the supplied systemd example or equivalent boot ordering before treating unattended restarts as safe.
- Direct `docker compose up` bypasses preflight; access to the host Docker Engine is an administrative privilege. Do not delegate deployment privileges or project config editing to the agent.
- The host's Tailscale identity is shared by all host-exposed services. Tailnet ACLs apply to reaching that *host*; there is no separate device identity/ACL for the container.
- Agents can install arbitrary code as `agent`, replace or leak their own files, initiate authorized public egress, compromise their own Codex auth and GitHub PAT, and run resource-consuming processes within configured limits.
- Docker networking can expose iptables subtleties (DNAT, IPv6, daemon restarts, upstream Docker rules). Verify with real-world deny tests from inside the sandbox and tailnet devices; static checks do not guarantee network isolation.
- `docker compose down` removes the container's writable layer but not external backups/snapshots, deleted blocks, Docker image layers, host journal logs, or server-side data uploaded to public APIs.
- Third-party build installer scripts (AGY, Oh My Zsh clone, npm) are executed during image build. Treat supply-chain authenticity, update cadence and version pinning as operational responsibilities.

## Non-goals

This is not a hardened multi-tenant sandbox, kernel security boundary, trusted secret manager, network traffic inspection system, Tailscale sidecar, or malicious-agent-proof environment. It does not offer per-agent filesystem isolation *within* the same Unix account, and it does not reliably prevent an authenticated agent from accessing its own persisted Codex credential or GitHub PAT.

## Recommended hardening beyond this template

- Run the entire workspace inside a dedicated VM/microVM, with a separate Tailscale device identity and least-privilege ACL.
- Store high-value credentials outside the agent VM and issue scoped/short-lived credentials through an authorization proxy where possible.
- Disable public egress by default, then allow only explicitly required services using network policy/proxy.
- Use separate sessions/containers per untrusted project; don't reuse secrets across trust zones.
- Audit Docker Engine changes, verify SSH host fingerprint on first connection and after deliberate key rotation.
