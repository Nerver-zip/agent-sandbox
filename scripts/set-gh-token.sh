#!/usr/bin/env bash
# Prompt for a fine-grained GitHub PAT. Never place the token on a command line.
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=agent-sandbox
FILE="$PWD/persistent/gh/token"
[[ -f "$FILE" && ! -L "$FILE" ]] || { echo 'Run scripts/setup.sh first.' >&2; exit 1; }
if ! [[ -t 0 ]]; then echo 'A terminal is required for secure token entry.' >&2; exit 1; fi
printf '%s' 'Fine-grained GitHub PAT (input hidden): ' >&2
IFS= read -rs TOKEN
printf '\n' >&2
[[ "$TOKEN" =~ ^(github_pat_|ghp_)[A-Za-z0-9_]+$ ]] || { unset TOKEN; echo 'Invalid PAT format. No changes made.' >&2; exit 1; }
# Truncate the existing mounted inode, even when a stopped container still mounts it.
if [[ $EUID -eq 0 ]]; then
  printf '%s\n' "$TOKEN" > "$FILE"
else
  printf '%s\n' "$TOKEN" | sudo tee "$FILE" >/dev/null
fi
unset TOKEN
if [[ $EUID -eq 0 ]]; then chown 10001:10001 "$FILE" && chmod 0400 "$FILE"
else sudo chown 10001:10001 "$FILE" && sudo chmod 0400 "$FILE"; fi
echo 'Fine-grained PAT saved. Revoke the token on GitHub if the workspace is compromised.'
