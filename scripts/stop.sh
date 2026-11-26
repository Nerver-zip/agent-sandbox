#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME=agent-sandbox
down=0
force=0
for arg in "$@"; do
  case "$arg" in
    --down) down=1 ;;
    --force) force=1 ;;
    *) echo "Usage: $0 [--down] [--force]" >&2; exit 2 ;;
  esac
done
if (( !force )); then
  ./scripts/token-monitor.sh final || {
    echo 'Token Monitor could not queue the final capture. Resume the workspace and retry, or use --force to proceed without accounting.' >&2
    exit 1
  }
fi
if (( down )); then
  docker compose down
else
  docker compose stop
fi
