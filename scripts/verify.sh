#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=agent-sandbox
fail() { echo "[FAIL] $*" >&2; exit 1; }
pass() { echo "[OK]   $*"; }
CID=$(docker compose ps -q workspace)
[[ -n "$CID" ]] || fail 'Container not found.'
[[ "$(docker inspect -f '{{.State.Running}}' "$CID")" == true ]] || fail 'Container not running.'
pass 'Container running'
[[ "$(docker inspect -f '{{.HostConfig.Privileged}}' "$CID")" == false ]] || fail 'Privileged container is forbidden.'
[[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$CID")" != host ]] || fail 'Host network is forbidden.'
[[ "$(docker inspect -f '{{len .Mounts}}' "$CID")" == 4 ]] || fail 'Expected only 4 persistent file mounts.'
MOUNTS=$(docker inspect -f '{{range .Mounts}}{{println .Destination}}{{end}}' "$CID" | grep -v '^$' | sort)
EXPECTED=$(printf '%s\n' /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key.pub /home/agent/.codex/auth.json /opt/agent-secrets/gh_token | sort)
[[ "$MOUNTS" == "$EXPECTED" ]] || fail 'Unexpected mounts detected.'
pass 'Exactly four allowlisted file mounts'
docker inspect -f '{{range .HostConfig.CapAdd}}{{println .}}{{end}}' "$CID" | grep -q 'AUDIT_WRITE' || fail 'AUDIT_WRITE required for SSHD'
pass 'SSHD audit capability present'
[[ $(stat -c '%u:%a' persistent/codex/auth.json) == 10001:600 ]] || fail 'Codex auth file ownership/permissions invalid'
pass 'Persisted Codex auth is UID 10001, mode 0600'
[[ $(stat -c '%u:%a' persistent/gh/token) == 10001:400 ]] || fail 'GitHub token permissions invalid'
pass 'Persisted GitHub PAT is UID 10001, mode 0400 (read-only mount)'
docker compose exec -T --user agent workspace bash -c \
  'test ! -r /etc/ssh/ssh_host_ed25519_key && test -r /opt/agent-secrets/gh_token && test ! -w /opt/agent-secrets/gh_token && test -w /home/agent/.codex/auth.json' \
  || fail 'Agent cannot access secrets with expected permissions.'
pass 'Agent cannot read SSH server private key or write GitHub PAT'

for bin in zsh nvim tmux starship fastfetch eza codex agy gh docker; do
  docker compose exec -T --user agent workspace bash -c "PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin command -v '$bin' >/dev/null" || fail "$bin is missing"
done
pass 'Zsh, Neovim, tmux, Starship, Fastfetch, Eza, Codex, AGY, GitHub CLI and Docker installed'
docker compose exec -T --user agent workspace bash -c 'test -f ~/.tmux.conf && grep -q "mouse on" ~/.tmux.conf' || fail 'tmux.conf missing or invalid'
pass 'Tmux configuration present and valid'

DIND_CID=$(docker compose ps -q dind)
[[ -n "$DIND_CID" ]] || fail 'DinD container not found.'
[[ "$(docker inspect -f '{{.State.Running}}' "$DIND_CID")" == true ]] || fail 'DinD container not running.'
BINDS=$(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source}}{{end}}{{end}}' "$DIND_CID" | grep -v '^$' || true)
[[ -z "$BINDS" ]] || fail "DinD must not mount host paths: $BINDS"
docker compose exec -T --user agent workspace docker info >/dev/null 2>&1 || fail 'Agent cannot connect to isolated Docker daemon.'
docker compose exec -T --user agent workspace env -u DOCKER_HOST docker info >/dev/null 2>&1 || fail 'Agent cannot connect via /var/run/docker.sock proxy.'
pass 'Isolated Docker daemon reachable via DOCKER_HOST and /var/run/docker.sock'
docker compose exec -T --user agent workspace bash -lc 'test "$(id -u)" -eq 10001 && test -w "$HOME/workspace"' || fail 'Agent user cannot write own workspace.'
[[ $(docker compose exec -T --user agent workspace getent passwd agent | tr -d '\r' | cut -d: -f7) == /usr/bin/zsh ]] || fail 'Zsh is not default shell'
pass 'Agent UID 10001 and Zsh default shell'
docker compose exec -T workspace /usr/sbin/sshd -t -f /etc/ssh/sshd_config || fail 'Invalid sshd_config.'
HOST_FP=$(ssh-keygen -lf persistent/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')
CONTAINER_FP=$(docker compose exec -T workspace ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')
[[ "$HOST_FP" == "$CONTAINER_FP" ]] || fail 'Host key mismatch'
pass 'Persisted SSH identity matches the running container'
if [[ $EUID -eq 0 ]]; then ./scripts/firewall.sh check; else sudo ./scripts/firewall.sh check; fi
pass 'Firewall policies installed'
if docker compose exec -T --user agent workspace curl -fsSIL --max-time 8 https://example.com/ >/dev/null 2>&1; then
  pass 'Public HTTPS reachable'
else
  echo '[WARN] Could not reach example.com; check DNS and egress.'
fi
printf '\nValidation complete. This is NOT a proof of VM-grade isolation.\n'
