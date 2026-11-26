#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
case "${1:-status}" in
  setup|status|sync|final|start|uninstall) action=${1:-status} ;;
  *) echo "Usage: $0 [setup|status|sync|uninstall]" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || exit 2
if [[ ! -e .token-monitor.json && ! -L .token-monitor.json && "$action" != uninstall ]]; then
  case "$action" in setup|status|sync) echo 'Token Monitor integration is disabled.' ;; esac
  exit 0
fi
runner=(/usr/bin/python3 -E -s "$PWD/scripts/token-monitor/manager.py" "$action")
if [[ "$action" == final ]]; then
  runner=(/usr/bin/timeout --foreground --kill-after=15s 240s "${runner[@]}")
fi
if (( EUID != 0 )); then
  exec sudo "${runner[@]}"
fi
exec "${runner[@]}"
