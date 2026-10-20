#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=agent-sandbox
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ -s .env && -s ssh/authorized_keys ]] || fail 'Run scripts/setup.sh first.'
[[ -s persistent/ssh/ssh_host_ed25519_key && -s persistent/ssh/ssh_host_ed25519_key.pub ]] || fail 'Host keys missing. Run setup.'
[[ -s persistent/ssh/authorized_keys ]] || fail 'Persistent authorized_keys missing. Run setup.'
[[ -f persistent/ssh/known_hosts ]] || fail 'SSH known_hosts file missing. Run setup.'
[[ -f persistent/codex/auth.json ]] || fail 'Codex credentials file missing. Run setup.'
[[ -f persistent/gh/token ]] || fail 'GitHub credential file missing. Run setup.'
[[ $(stat -c '%u:%a' persistent/gh/token) == 10001:400 ]] || fail 'GitHub token must be owned by UID 10001, mode 0400.'
[[ $(stat -c '%u:%a' persistent/codex/auth.json) == 10001:600 ]] || fail 'Codex auth file must be owned by UID 10001, mode 0600.'
[[ $(stat -c '%u:%a' persistent/ssh/authorized_keys) == 10001:600 ]] || fail 'Persistent authorized_keys must be owned by UID 10001, mode 0600.'
[[ $(stat -c '%u:%a' persistent/ssh/known_hosts) == 10001:600 ]] || fail 'SSH known_hosts file must be owned by UID 10001, mode 0600.'
command -v docker >/dev/null || fail 'Docker not installed.'
command -v tailscale >/dev/null || fail 'Tailscale not installed.'
command -v ip >/dev/null || fail 'iproute2 not installed.'
[[ -z "${DOCKER_HOST:-}" ]] || fail 'Remote DOCKER_HOST not supported (firewall must run on Docker host).'
DOCKER_ENDPOINT=$(docker context inspect "$(docker context show)" --format '{{ .Endpoints.docker.Host }}')
[[ "$DOCKER_ENDPOINT" == 'unix:///var/run/docker.sock' || "$DOCKER_ENDPOINT" == 'unix:///run/docker.sock' ]] || fail "Non-local Docker endpoint: $DOCKER_ENDPOINT"
DOCKER_SECURITY=$(docker info --format '{{json .SecurityOptions}}')
[[ "$DOCKER_SECURITY" != *rootless* ]] || fail 'Docker rootless is not supported with DOCKER-USER rules.'
TS_EXPECTED=$(sed -n 's/^TAILSCALE_IP=//p' .env | tail -n 1)
TS_CURRENT=$(tailscale ip -4 2>/dev/null | head -n 1)
[[ -n "$TS_CURRENT" && "$TS_EXPECTED" == "$TS_CURRENT" ]] || fail 'Tailscale host address mismatch. Re-run setup.'
ip link show tailscale0 >/dev/null 2>&1 || fail 'tailscale0 interface not found.'
ip -4 -o addr show dev tailscale0 | grep -Fq " $TS_CURRENT/" || fail 'Tailscale IP is not bound to tailscale0.'

NETWORK=agent_sandbox_net
BRIDGE=br-agentsbx
SUBNET=172.30.247.0/24
if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  docker network create --driver bridge --subnet "$SUBNET" \
    --opt "com.docker.network.bridge.name=$BRIDGE" "$NETWORK" >/dev/null
fi
ACTUAL_BR=$(docker network inspect -f '{{ index .Options "com.docker.network.bridge.name" }}' "$NETWORK")
ACTUAL_SUBNET=$(docker network inspect -f '{{ (index .IPAM.Config 0).Subnet }}' "$NETWORK")
[[ "$ACTUAL_BR" == "$BRIDGE" && "$ACTUAL_SUBNET" == "$SUBNET" ]] || fail 'Existing network does not have expected bridge/subnet.'
EXTRA=$(docker network inspect -f '{{range $id, $c := .Containers}}{{println $c.Name}}{{end}}' "$NETWORK" | grep -v -E '^agent-sandbox-(workspace|dind)-1$' || true)
[[ -z "$EXTRA" ]] || fail "Unexpected containers attached to sandbox network: $EXTRA"

# Fail-closed: network firewall must be installed BEFORE Docker container starts.
if [[ $EUID -eq 0 ]]; then
  ./scripts/firewall.sh install
  ./scripts/firewall.sh check
else
  sudo ./scripts/firewall.sh install
  sudo ./scripts/firewall.sh check
fi
docker compose config --quiet
docker compose up --build -d --remove-orphans
PORT=$(sed -n 's/^AGENT_SSH_PORT=//p' .env | tail -n 1)
printf '\nAgent Sandbox ready: ssh -p %s agent@%s\n' "${PORT:-2222}" "$TS_CURRENT"
echo 'Container filesystem is disposable. SSH host identity, Codex auth and GitHub PAT persist.'
