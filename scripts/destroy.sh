#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=agent-sandbox
cat <<'TEXT'
DANGER: This permanently deletes the Agent Sandbox container, ephemeral workspace,
SSH host identity, Codex login tokens and GitHub PAT. You may need to authenticate again.
Other Docker projects are not targeted. Global Docker build cache is not purged.
TEXT
if [[ "${1:-}" != '--yes' ]]; then
  read -r -p 'Type DESTROY to proceed: ' reply
  [[ "$reply" == 'DESTROY' ]] || { echo 'Cancelled.'; exit 1; }
fi
# Down must happen while Compose .env and bind sources still exist.
docker compose down --remove-orphans --volumes
# Clear firewall FIRST (does not affect unrelated projects).
if [[ $EUID -eq 0 ]]; then ./scripts/firewall.sh remove; else sudo ./scripts/firewall.sh remove; fi
# Remove only this project's isolated network.
if docker network inspect agent_sandbox_net >/dev/null 2>&1; then
  docker network rm agent_sandbox_net
fi
# Image is reconstructible; remove it on explicit destructive reset.
docker image rm agent-sandbox:local >/dev/null 2>&1 || echo 'Note: image still in use or already absent.'
# Remove only generated state in this project, not parent directories.
rm -f -- persistent/ssh/ssh_host_ed25519_key persistent/ssh/ssh_host_ed25519_key.pub
# Token and user SSH files are owned by container UID 10001, so host operator may need sudo.
if [[ $EUID -eq 0 ]]; then
  rm -f -- persistent/codex/auth.json persistent/gh/token persistent/ssh/authorized_keys persistent/ssh/known_hosts
else
  sudo rm -f -- persistent/codex/auth.json persistent/gh/token persistent/ssh/authorized_keys persistent/ssh/known_hosts
fi
rm -f -- ssh/authorized_keys .env
# Also clean up SSH host keypair from older releases.
rm -f -- ssh/host_keys/ssh_host_ed25519_key ssh/host_keys/ssh_host_ed25519_key.pub
rmdir ssh/host_keys 2>/dev/null || true
rmdir persistent/ssh persistent/codex persistent/gh persistent 2>/dev/null || true
echo 'Agent Sandbox identity, credentials, container and generated configuration removed.'
