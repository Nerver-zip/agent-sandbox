#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 ]]; then
  echo 'Usage: ./scripts/import-claude-auth.sh /path/to/.credentials.json' >&2
  exit 2
fi
SOURCE=$1
[[ -f "$SOURCE" && ! -L "$SOURCE" ]] || { echo 'Source must be a regular file, not a symlink.' >&2; exit 1; }
[[ -f persistent/claude/.credentials.json ]] || { echo 'Run scripts/setup.sh first.' >&2; exit 1; }
python3 -m json.tool "$SOURCE" >/dev/null || { echo 'Invalid JSON source.' >&2; exit 1; }
# Apply only while stopped, avoiding races with Claude token refresh.
if docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | grep -q .; then
  echo 'Stop the sandbox with docker compose stop before importing credentials.' >&2
  exit 1
fi
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cat "$SOURCE" > "$TMP"
# DO NOT replace bind-mounted inode: write in-place.
if [[ $EUID -eq 0 ]]; then cat "$TMP" > persistent/claude/.credentials.json
else sudo sh -c 'cat "$1" > "$2"' sh "$TMP" "$PWD/persistent/claude/.credentials.json"
fi
printf 'Imported Claude Code authentication. Secrets are readable by the agent user.\n'
