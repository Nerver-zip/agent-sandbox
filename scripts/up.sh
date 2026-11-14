#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=agent-sandbox
fail() { echo "ERROR: $*" >&2; exit 1; }
REBUILD=0
for arg in "$@"; do
  case "$arg" in
    --rebuild|-r)
      REBUILD=1
      ;;
    *)
      fail "Unknown option: $arg. Usage: $0 [--rebuild]"
      ;;
  esac
done
[[ -s .env && -s ssh/authorized_keys ]] || fail 'Run scripts/setup.sh first.'
[[ -s persistent/ssh/ssh_host_ed25519_key && -s persistent/ssh/ssh_host_ed25519_key.pub ]] || fail 'Host keys missing. Run setup.'
[[ -s persistent/ssh/authorized_keys ]] || fail 'Persistent authorized_keys missing. Run setup.'
[[ -f persistent/ssh/known_hosts ]] || fail 'SSH known_hosts file missing. Run setup.'
[[ -d persistent/codex && ! -L persistent/codex ]] || fail 'Codex credential directory missing or unsafe. Run setup.'
[[ -f persistent/claude/.credentials.json ]] || fail 'Claude credentials file missing. Run setup.'
[[ -f persistent/gh/token ]] || fail 'GitHub credential file missing. Run setup.'
[[ $(stat -c '%u:%a' persistent/gh/token) == 10001:400 ]] || fail 'GitHub token must be owned by UID 10001, mode 0400.'
[[ $(stat -c '%u:%a' persistent/codex) == 10001:700 ]] || fail 'Codex credential directory must be owned by UID 10001, mode 0700.'
if [[ $EUID -eq 0 ]]; then
  CODEX_AUTH_STATE=$(stat -c '%F:%u:%a:%h' persistent/codex/auth.json 2>/dev/null || true)
  CODEX_LOCK_STATE=$(stat -c '%F:%u:%a' persistent/codex/auth.lock 2>/dev/null || true)
  CODEX_OPERATION_STATE=$(stat -c '%F:%u:%a' persistent/codex/auth-operation 2>/dev/null || true)
  CODEX_ENTRIES=$(find persistent/codex -mindepth 1 -maxdepth 1 -printf '%f\n')
else
  sudo -v || fail 'sudo is required to inspect the protected Codex credential directory.'
  CODEX_AUTH_STATE=$(sudo stat -c '%F:%u:%a:%h' persistent/codex/auth.json 2>/dev/null || true)
  CODEX_LOCK_STATE=$(sudo stat -c '%F:%u:%a' persistent/codex/auth.lock 2>/dev/null || true)
  CODEX_OPERATION_STATE=$(sudo stat -c '%F:%u:%a' persistent/codex/auth-operation 2>/dev/null || true)
  CODEX_ENTRIES=$(sudo find persistent/codex -mindepth 1 -maxdepth 1 -printf '%f\n')
fi
[[ "$CODEX_AUTH_STATE" == 'regular file:10001:600:1' ]] || fail 'Codex auth file must be a regular UID 10001 file, mode 0600, without hard links.'
[[ "$CODEX_LOCK_STATE" == 'regular file:10001:600' ]] || fail 'Codex auth lock must be a regular UID 10001 file, mode 0600.'
[[ -z "$CODEX_OPERATION_STATE" || "$CODEX_OPERATION_STATE" == 'regular file:10001:600' ]] || fail 'Codex auth operation marker has unsafe type, ownership, or permissions.'
while IFS= read -r entry; do
  case "$entry" in
    auth.json|auth.lock|auth-operation|.auth-*) ;;
    *) fail "Unexpected file in the Codex credential directory: $entry" ;;
  esac
done <<< "$CODEX_ENTRIES"
[[ $(stat -c '%u:%a' persistent/claude/.credentials.json) == 10001:600 ]] || fail 'Claude credentials file must be owned by UID 10001, mode 0600.'
[[ $(stat -c '%u:%a' persistent/ssh/authorized_keys) == 10001:600 ]] || fail 'Persistent authorized_keys must be owned by UID 10001, mode 0600.'
[[ $(stat -c '%u:%a' persistent/ssh/known_hosts) == 10001:600 ]] || fail 'SSH known_hosts file must be owned by UID 10001, mode 0600.'
[[ -f persistent/git/config ]] || fail 'Git config file missing. Run setup.'
[[ $(stat -c '%u:%a' persistent/git/config) == 10001:600 ]] || fail 'Git config file must be owned by UID 10001, mode 0600.'
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
EXISTING_WS=$(docker compose ps -a -q workspace 2>/dev/null || true)
EXISTING_DIND=$(docker compose ps -a -q dind 2>/dev/null || true)
if [[ $REBUILD -eq 1 || -z "$EXISTING_WS" || -z "$EXISTING_DIND" ]]; then
  docker compose up --build -d --force-recreate --remove-orphans
else
  RUNNING_WS=$(docker compose ps -q workspace 2>/dev/null || true)
  RUNNING_DIND=$(docker compose ps -q dind 2>/dev/null || true)
  if [[ -n "$RUNNING_WS" && -n "$RUNNING_DIND" ]]; then
    echo 'Agent Sandbox containers are already running (workspace preserved).'
  else
    echo 'Starting existing Agent Sandbox containers (preserving workspace)...'
    docker compose start
  fi
fi
PORT=$(sed -n 's/^AGENT_SSH_PORT=//p' .env | tail -n 1)
printf '\nAgent Sandbox ready: ssh -p %s agent@%s\n' "${PORT:-2222}" "$TS_CURRENT"
echo 'Container filesystem is disposable. SSH host identity, Codex auth, Claude credentials, Git config and GitHub PAT persist.'
