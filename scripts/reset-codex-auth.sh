#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f persistent/codex/auth.json ]] || { echo 'No persisted Codex auth file.' >&2; exit 1; }
if docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | grep -q .; then
  echo 'Stop the sandbox with docker compose stop before resetting credentials.' >&2
  exit 1
fi
if [[ $EUID -eq 0 ]]; then printf '{}\n' > persistent/codex/auth.json
else sudo sh -c 'printf "{}\\n" > "$1"' sh "$PWD/persistent/codex/auth.json"
fi
echo 'Persisted Codex authentication reset. Start sandbox and log in again.'
