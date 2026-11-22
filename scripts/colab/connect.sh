#!/usr/bin/env bash
# Open an interactive SSH session (or run one command) on the Colab runtime.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

FORCE=0
VERBOSE=0
COMMAND=()

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/connect.sh [options] [-- command [args...]]

Options:
  --force     Clear stale local Colab SSH proxy processes before connecting
  -v, --verbose
              Show the local ssh command details
  -h, --help  Show this help

Examples:
  ./scripts/colab/connect.sh
  ./scripts/colab/connect.sh -- tmux new -As agents
  ./scripts/colab/connect.sh -- bash -lc 'cd ~/workspace && git status'
TEXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    -v|--verbose) VERBOSE=1 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; COMMAND=("$@"); break ;;
    *) die "Unknown option: $1. Use --help (put remote commands after --)." ;;
  esac
  shift
done

load_config
require_existing_session
[[ $FORCE -eq 1 ]] && clear_stale_ssh_proxies
write_ssh_config

SSH_ARGS=(-F "$COLAB_SSH_CONFIG" -o ConnectTimeout=15)
if [[ $VERBOSE -eq 1 ]]; then
  SSH_ARGS+=(-v)
fi

if [[ ${#COMMAND[@]} -gt 0 ]]; then
  exec ssh "${SSH_ARGS[@]}" "$COLAB_HOST" "${COMMAND[@]}"
fi
exec ssh "${SSH_ARGS[@]}" "$COLAB_HOST"
