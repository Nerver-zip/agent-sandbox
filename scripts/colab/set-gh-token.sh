#!/usr/bin/env bash
# shellcheck disable=SC2016
# Send an optional GitHub PAT to the running Colab runtime without storing it locally.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/set-gh-token.sh

Prompts for a fine-grained GitHub PAT (or reads one line from stdin when not a
TTY), then authenticates `gh` inside the runtime. The token is never written to
the local repository, argv, or shell history by this script. It is stored only
on the disposable runtime.

To use normal interactive browser/device login instead, run ./scripts/colab/connect.sh
and then `gh auth login` inside the runtime.
TEXT
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) die "Unknown option: $1. Use --help." ;;
esac

load_config
AGENT_COLAB_NONINTERACTIVE=1
require_existing_session
write_ssh_config

TOKEN=
if [[ -t 0 ]]; then
  printf 'Fine-grained GitHub PAT (input hidden): ' >&2
  IFS= read -rs TOKEN || TOKEN=
  printf '\n' >&2
else
  IFS= read -r TOKEN || TOKEN=
fi
TOKEN=${TOKEN%$'\r'}
if [[ ! $TOKEN =~ ^(github_pat_|ghp_)[A-Za-z0-9_]+$ ]]; then
  unset TOKEN
  die 'Invalid PAT format. No changes were made.'
fi

# The runtime permits only one SSH session at a time, so perform the presence
# check, login, and credential-helper setup in a single connection.
REMOTE_COMMAND='
command -v gh >/dev/null 2>&1 || exit 42
IFS= read -r GH_TOKEN || exit 43
printf "%s\n" "$GH_TOKEN" | gh auth login --with-token || exit 44
gh auth setup-git || exit 45
'
rc=0
printf '%s\n' "$TOKEN" \
  | ssh -T -F "$COLAB_SSH_CONFIG" -o BatchMode=yes -o ConnectTimeout=10 "$COLAB_HOST" "$REMOTE_COMMAND" \
  || rc=$?
unset TOKEN

case $rc in
  0) ;;
  42) die 'GitHub CLI is not installed in the runtime. Run ./scripts/colab/up.sh first.' ;;
  43) die 'No token was received. No changes were made.' ;;
  44) die 'GitHub authentication failed. The token was not written locally.' ;;
  45)
    warn 'The token was accepted, but `gh auth setup-git` failed. Run it manually inside the runtime.'
    log 'GitHub CLI authenticated inside the disposable runtime.'
    exit 0
    ;;
  *)
    die "Could not complete GitHub authentication (ssh exit $rc). No token was written locally."
    ;;
esac

log 'GitHub CLI authenticated inside the disposable runtime.'
log 'The token exists only on the runtime. Revoke it in GitHub if the runtime is compromised.'
