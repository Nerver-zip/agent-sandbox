#!/usr/bin/env bash
# shellcheck disable=SC2016
# Explicitly release the tracked Google Colab runtime.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

YES=0
SAVE_FIRST=0

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/down.sh [options]

Options:
  --yes       Do not ask for confirmation
  --save      Archive the remote workspace locally before stopping
  -h, --help  Show this help

Only the session tracked in .colab/config.env is stopped. Other Colab
sessions are never targeted.
TEXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) YES=1 ;;
    --save) SAVE_FIRST=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1. Use --help." ;;
  esac
  shift
done

load_config
acquire_lock
trap 'release_lock' EXIT

if ! check_auth_and_show_usage >/dev/null; then
  die 'Could not verify Google authentication; nothing was stopped.'
fi

rc=0
session_exists || rc=$?
case $rc in
  0) ;;
  1)
    clear_runtime_metadata
    release_lock
    trap - EXIT
    log "No active Colab runtime named '$COLAB_SESSION'."
    exit 0
    ;;
  3)
    die "The recorded endpoint '$COLAB_ENDPOINT' is still active, but its local session entry is gone. Inspect 'colab sessions' and stop it explicitly. No other session was touched."
    ;;
  *)
    die 'Could not query Colab sessions. Nothing was stopped.'
    ;;
esac

printf 'Runtime %s is about to be terminated.\n' "$COLAB_SESSION"
printf 'Its workspace and all runtime credentials will be destroyed. Local .colab state and archives are kept.\n'
if [[ $SAVE_FIRST -eq 0 ]]; then
  printf 'Save first with: ./scripts/colab/save.sh\n'
fi

if [[ $SAVE_FIRST -eq 1 ]]; then
  "$SCRIPT_DIR/save.sh" || die 'Workspace archive failed; the runtime was left running.'
fi

if [[ $YES -ne 1 ]]; then
  [[ -t 0 ]] || die 'Refusing to stop without confirmation in a non-interactive shell; pass --yes.'
  printf 'Type "stop" to terminate %s: ' "$COLAB_SESSION" >&2
  IFS= read -r reply || reply=
  [[ $reply == stop ]] || { log 'Cancelled.'; exit 0; }
fi

if ! run_colab stop -s "$COLAB_SESSION"; then
  die '`colab stop` failed. The runtime may still be active; check `colab sessions` and ./scripts/colab/status.sh.'
fi

confirmed=0
attempt=1
while [[ $attempt -le 3 ]]; do
  rc=0
  session_exists || rc=$?
  case $rc in
    1) confirmed=1; break ;;
    0) ;;
    3) ;;
    *) break ;;
  esac
  [[ $attempt -lt 3 ]] && sleep 2
  attempt=$((attempt + 1))
done

if [[ $confirmed -ne 1 ]]; then
  warn 'The session is still listed after `colab stop`. It may be in a transient state.'
  warn 'Run ./scripts/colab/status.sh and, if needed, `colab sessions` before creating another runtime.'
  exit 1
fi

clear_runtime_metadata
release_lock
trap - EXIT

log "Colab runtime '$COLAB_SESSION' terminated."
log 'Local SSH key, configuration, and workspace archives were kept.'
