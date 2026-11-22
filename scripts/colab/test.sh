#!/usr/bin/env bash
# Run the repository's static test suite inside the active Colab runtime.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/test.sh

Copies the local repository (excluding .git, .colab, backups, persistent, and
Python caches) to the active Colab runtime and runs:

    python3 -m unittest discover -s tests -v

The remote copy lives under /tmp and disappears with the runtime. The runtime
must already be provisioned with ./scripts/colab/up.sh.
TEXT
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) die "Unknown option: $1. Use --help." ;;
esac

load_config
require_existing_session
write_ssh_config

REMOTE_ROOT=/tmp/agent-sandbox-tests

log "Copying the test suite to '$COLAB_SESSION' and running it..."
tar -C "$COLAB_REPO_ROOT" -czf - \
  --exclude='./.git' \
  --exclude='./.colab' \
  --exclude='./.env' \
  --exclude='./backups' \
  --exclude='./persistent' \
  --exclude='./ssh' \
  --exclude='./GOAL.md' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  . \
  | ssh_run "set -euo pipefail
    rm -rf '$REMOTE_ROOT'
    mkdir -p '$REMOTE_ROOT'
    tar -xzf - -C '$REMOTE_ROOT'
    cd '$REMOTE_ROOT'
    if ! python3 -c 'import yaml' >/dev/null 2>&1; then
      python3 -m pip install --quiet --break-system-packages PyYAML \
        || python3 -m pip install --quiet --user PyYAML
    fi
    python3 -m unittest discover -s tests -v"

log 'Colab test suite completed.'