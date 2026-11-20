#!/usr/bin/env bash
# Archive the remote workspace to the local machine before releasing a runtime.
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/save.sh [DESTINATION]

DESTINATION defaults to backups/colab-workspace-<UTC timestamp>.tar.gz.
Use "-" to stream the gzip archive to stdout.

The archive contains the complete remote workspace, including untracked files
and any secrets you placed there. Commit and push important changes to Git as
the primary persistence mechanism.
TEXT
}

case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  '') DEST="$COLAB_REPO_ROOT/backups/colab-workspace-$(date -u +%Y%m%dT%H%M%SZ).tar.gz" ;;
  -) DEST=- ;;
  *) DEST=$1 ;;
esac

load_config
require_existing_session
write_ssh_config

REMOTE_PARENT=$(dirname "$COLAB_WORKSPACE")
REMOTE_BASE=$(basename "$COLAB_WORKSPACE")
REMOTE_CMD="tar -C $REMOTE_PARENT -czf - $REMOTE_BASE"

if [[ $DEST == - ]]; then
  printf 'Streaming %s from %s as a gzip tar archive.\n' "$COLAB_WORKSPACE" "$COLAB_SESSION" >&2
  exec ssh -T -F "$COLAB_SSH_CONFIG" -o BatchMode=yes -o ConnectTimeout=10 "$COLAB_HOST" "$REMOTE_CMD"
fi

DEST_DIR=$(dirname "$DEST")
if [[ ! -d $DEST_DIR ]]; then
  mkdir -p "$DEST_DIR"
  chmod 0700 "$DEST_DIR"
fi

TMP="$DEST.partial.$$.$RANDOM"
cleanup() { rm -f -- "$TMP"; }
trap cleanup EXIT

if ! ssh -T -F "$COLAB_SSH_CONFIG" -o BatchMode=yes -o ConnectTimeout=10 "$COLAB_HOST" "$REMOTE_CMD" > "$TMP"; then
  die 'Could not download the remote workspace. The runtime was not changed.'
fi
gzip -t "$TMP" 2>/dev/null || die 'The downloaded workspace archive is not valid gzip.'
chmod 0600 "$TMP"
mv -f -- "$TMP" "$DEST"
trap - EXIT

log "Saved $COLAB_WORKSPACE to $DEST ($(human_size "$DEST") size)."
log 'The archive may contain credentials or other secrets; it is owner-readable only.'
log 'Prefer Git commits and pushes for work you need to keep long term.'
