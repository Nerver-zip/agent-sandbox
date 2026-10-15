#!/usr/bin/env bash
# Clear only the host-persisted GitHub PAT; the shell/workspace remains disposable.
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="$PWD/persistent/gh/token"
[[ -f "$FILE" && ! -L "$FILE" ]] || { echo 'GitHub token is not initialized.' >&2; exit 1; }
if [[ $EUID -eq 0 ]]; then : > "$FILE"; else sudo sh -c ': > "$1"' sh "$FILE"; fi
echo 'Local PAT cleared. Also revoke it from GitHub if it may have leaked.'
