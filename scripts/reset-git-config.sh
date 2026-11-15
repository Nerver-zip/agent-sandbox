#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
GIT_CONFIG_FILE="$PWD/persistent/git/config"
if docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | grep -q .; then
  echo 'Stop the sandbox with docker compose stop before resetting Git configuration.' >&2
  exit 1
fi
[[ -f "$GIT_CONFIG_FILE" ]] || { echo 'Run scripts/setup.sh first.' >&2; exit 1; }
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<EOF
[init]
	defaultBranch = main
EOF
if [[ -w "$GIT_CONFIG_FILE" ]]; then
  cat "$TMP" > "$GIT_CONFIG_FILE"
  chmod 0600 "$GIT_CONFIG_FILE" 2>/dev/null || true
elif [[ $EUID -eq 0 ]]; then
  cat "$TMP" > "$GIT_CONFIG_FILE"
  chown 10001:10001 "$GIT_CONFIG_FILE"
  chmod 0600 "$GIT_CONFIG_FILE"
else
  sudo sh -c 'cat "$1" > "$2" && chown 10001:10001 "$2" && chmod 0600 "$2"' sh "$TMP" "$GIT_CONFIG_FILE"
fi
echo 'Persisted Git identity reset. Run ./scripts/import-git-config.sh or re-run setup to configure.'
