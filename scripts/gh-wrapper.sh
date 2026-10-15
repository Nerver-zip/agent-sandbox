#!/usr/bin/env bash
# Use the fine-grained PAT without keeping it in shell startup variables or gh config.
set -euo pipefail
TOKEN_FILE=/opt/agent-secrets/gh_token
if [[ -s "$TOKEN_FILE" ]]; then
  [[ -r "$TOKEN_FILE" ]] || { echo 'GitHub token is not readable by the agent.' >&2; exit 1; }
  export GH_TOKEN="$(cat "$TOKEN_FILE")"
fi
exec /usr/bin/gh "$@"
