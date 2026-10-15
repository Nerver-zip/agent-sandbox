# Security model and operational boundaries

## Goals

The unprivileged `agent` should not receive the host's SSH private key, Docker socket, homelab bind mounts, production files, root login or a privileged container. A dedicated Docker bridge and host iptables `DOCKER-USER`/`INPUT` rules block **new connections** from this bridge to other RFC1918 networks, CGNAT/tailnet (`100.64/10`), loopback, link-local, host services, and other Docker bridges. SSH exposure is restricted to the host's Tailscale IPv4 address. CPU, RAM, and process limits reduce accidental resource exhaustion.

## Explicit exceptions

1. SSH host keypair is mounted read-only into the container for SSHD to read as root. The `agent` user cannot directly read the private key by ordinary Unix permissions.
2. `persistent/codex/auth.json` is mounted read-write as user UID 10001. Anyone executing as `agent` can read it and can potentially send it to public Internet services. There is **no safe way** to promise both unrestricted agent commands and secrets that the agent cannot access while its own process needs them.
3. `persistent/gh/token` is a fine-grained PAT mounted read-only, owned by UID 10001. The agent can read and exfiltrate it, even if the wrapper keeps it out of the global login shell environment. Github PAT scopes and expiration must be tightly restricted. The official CLI (`/usr/bin/gh`) is not a safety boundary: agents can run it, the wrapper, or directly read the token file.
4. Public HTTPS egress is allowed so agents can download packages, clone public Git repos and contact external AI APIs. That also allows data exfiltration and public-network scanning. The firewall is not a general domain allowlist, proxy or content filter.
5. The embedded Docker DNS resolver may provide DNS responses. Docker's DNS proxy can have configuration-dependent behavior; do not regard DNS isolation as preventing every form of internal name or metadata discovery.
6. `authorized_keys` (a *public* key) is copied into the Docker image. It does not contain a private key.

## Limits and known risks

- Docker shares the host's Linux kernel; a container escape vulnerability could break isolation. For genuinely untrusted code, use a VM/microVM with separate Tailscale identity and egress controls, ideally on a separate host or VLAN.
- Root inside a Docker container is not equivalent to root on the host, but the SSHD process must start as root and retain a few capabilities; it then drops to the unprivileged `agent` account on login.
- There are exactly four allowlisted bind-mounted files (SSH keypair, Codex auth, GitHub token). Do not add more just for convenience without reviewing the trust boundary.
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
