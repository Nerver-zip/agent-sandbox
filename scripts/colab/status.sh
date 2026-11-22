#!/usr/bin/env bash
# shellcheck disable=SC2016
# Show Colab runtime state, bootstrap freshness, and compute-unit usage.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/status.sh

Read-only. Never provisions, starts, or stops a runtime.
TEXT
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  '') ;;
  *) die "Unknown option: $1. Use --help." ;;
esac

load_config
AGENT_COLAB_NONINTERACTIVE=1

log "Colab session: $COLAB_SESSION"
if [[ -n $COLAB_ENDPOINT ]]; then
  log "Recorded endpoint: $COLAB_ENDPOINT"
else
  log 'Recorded endpoint: (none)'
fi
printf '\n'

if usage_output=$(check_auth_and_show_usage); then
  printf '%s\n' "$usage_output"
else
  warn 'Could not read compute-unit usage. Complete Google authentication with `colab usage` if needed.'
fi
printf '\n'

rc=0
session_exists || rc=$?
case $rc in
  0)
    STATUS_LINE=$(session_status_line) || die 'The runtime is listed but its status could not be read.'
    log "Runtime: $STATUS_LINE"
    ;;
  1)
    log "No active Colab runtime named '$COLAB_SESSION'."
    log 'Run ./scripts/colab/up.sh to provision one.'
    exit 0
    ;;
  3)
    die "The recorded endpoint '$COLAB_ENDPOINT' is still active, but its local session entry is gone. Inspect 'colab sessions' and resolve it explicitly."
    ;;
  *)
    die 'Could not query Colab sessions.'
    ;;
esac

write_ssh_config
if ! REMOTE_STATUS=$(ssh_run 'bash -s' <<'REMOTE'
set -u
export PATH=/usr/local/bin:/usr/bin:/bin:/root/.local/bin
printf 'Host: %s\n' "$(hostname 2>/dev/null || printf unknown)"
printf 'Kernel: %s\n' "$(uname -sr 2>/dev/null || printf unknown)"
if uptime -p >/dev/null 2>&1; then
  printf 'Uptime: %s\n' "$(uptime -p)"
else
  printf 'Uptime: %s\n' "$(uptime 2>/dev/null | sed 's/^ *//' || printf unknown)"
fi
printf 'CPUs: %s\n' "$(nproc 2>/dev/null || printf unknown)"
printf 'Memory: %s\n' "$(free -h 2>/dev/null | awk '/^Mem:/{print $3 " used / " $2 " total"}' || printf unknown)"
printf 'Disk: %s\n' "$(df -h / 2>/dev/null | awk 'NR==2{print $3 " used / " $2 " total (" $5 ")"}' || printf unknown)"
if [[ -d /home/agent/workspace ]]; then
  printf 'Workspace: %s\n' "$(du -sh /home/agent/workspace 2>/dev/null | awk '{print $1}')"
else
  printf 'Workspace: missing\n'
fi
printf 'Bootstrap: %s\n' "$(cat /root/.agent-sandbox/bootstrap-version 2>/dev/null || printf missing)"
printf 'Cache: %s\n' "$(cat /root/.agent-sandbox/nvim-cache-key 2>/dev/null || printf none)"
if command -v nvidia-smi >/dev/null 2>&1; then
  GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -n 1 || true)
  [[ -n ${GPU:-} ]] && printf 'GPU: %s\n' "$GPU"
fi
check() {
  local bin=$1
  shift
  if command -v "$bin" >/dev/null 2>&1; then
    printf '%s: ' "$bin"
    "$@" 2>/dev/null | head -n 1 || true
  else
    printf '%s: missing\n' "$bin"
  fi
}
check zsh zsh --version
check tmux tmux -V
check nvim nvim --version
check starship starship --version
check eza eza --version
check fastfetch fastfetch --version
check node node --version
check python3 python3 --version
check git git --version
check cmake cmake --version
check gh gh --version
check gitleaks gitleaks version
check codex codex --version
check claude claude --version
check opencode opencode --version
check agy agy --version
REMOTE
); then
  warn 'The runtime is listed but the SSH health check failed. It may still be starting or the SSH session limit may be exhausted.'
  warn 'Try ./scripts/colab/connect.sh --force, or ./scripts/colab/down.sh to release it.'
  exit 1
fi

printf '%s\n' "$REMOTE_STATUS"
MARKER=$(printf '%s\n' "$REMOTE_STATUS" | sed -n 's/^Bootstrap: //p' | head -n 1)
LOCAL_HASH=$(payload_hash)
printf '\n'
if [[ $MARKER == "$LOCAL_HASH" ]]; then
  log 'Bootstrap state: current.'
else
  log "Bootstrap state: stale (remote ${MARKER:-missing}, local $LOCAL_HASH)."
  log 'Run ./scripts/colab/up.sh to refresh it.'
fi
printf '\n'
if [[ ${COLAB_CACHE:-1} == 1 ]]; then
  cache_summary
else
  log 'Environment cache: disabled.'
fi
