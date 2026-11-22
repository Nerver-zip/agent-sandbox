#!/usr/bin/env bash
# Internal OpenSSH ProxyCommand wrapper. It refuses to auto-create a runtime.
set -euo pipefail
AGENT_COLAB_STDERR=1

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

if [[ $# -gt 0 ]]; then
  die "proxy.sh takes no arguments, got: $1"
fi

load_config
[[ -f $COLAB_SSH_KEY ]] || die "Dedicated Colab SSH key is missing: $COLAB_SSH_KEY"

rc=0
session_exists || rc=$?
case $rc in
  0) ;;
  1) die "No active Colab runtime named '$COLAB_SESSION'. Run ./scripts/colab/up.sh. The proxy will not create a runtime." ;;
  3) die "The recorded endpoint '$COLAB_ENDPOINT' is still active but its local session entry is gone. Inspect 'colab sessions' and resolve it explicitly. The proxy will not create a runtime." ;;
  *) die 'Could not query Colab sessions; refusing to connect. The proxy will not create a runtime.' ;;
esac

build_colab_argv
PROXY_ARGV=("${COLAB_ARGV[@]}" ssh --proxy-mode -s "$COLAB_SESSION" --identity "$COLAB_SSH_KEY")
exec "${PROXY_ARGV[@]}"
