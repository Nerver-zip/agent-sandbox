#!/usr/bin/env bash
# Restore a workspace archive produced by save.sh into the Colab runtime.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

FORCE=0
REPLACE_EMPTY=0
ARCHIVE=

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/restore.sh [--force|--replace-empty] ARCHIVE

By default the archive is extracted to /home/agent/workspace.restore-<UTC>.
With --force it is merged into the live workspace, overwriting matching files.
With --replace-empty it is extracted into the live workspace only when that
workspace is empty; otherwise the restore is refused.

The runtime must already be provisioned with ./scripts/colab/up.sh.
TEXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force) FORCE=1 ;;
    --replace-empty) REPLACE_EMPTY=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "Unknown option: $1. Use --help." ;;
    *)
      [[ -z $ARCHIVE ]] || die 'Pass exactly one archive path.'
      ARCHIVE=$1
      ;;
  esac
  shift
done

[[ -n $ARCHIVE ]] || { usage >&2; exit 2; }
[[ $FORCE -eq 0 || $REPLACE_EMPTY -eq 0 ]] || die 'Use either --force or --replace-empty, not both.'
[[ -f $ARCHIVE && -r $ARCHIVE ]] || die "Archive is not readable: $ARCHIVE"
tar -tzf "$ARCHIVE" >/dev/null 2>&1 || die 'Archive is not a valid gzip tar file.'
load_config
REMOTE_BASE=$(basename "$COLAB_WORKSPACE")
FIRST_ENTRY=$(tar -tzf "$ARCHIVE" | sed -n '1p')
[[ $FIRST_ENTRY == "$REMOTE_BASE"/* ]] || die "Archive does not contain a top-level $REMOTE_BASE/ directory; refusing to extract it."

require_existing_session
write_ssh_config

if [[ $REPLACE_EMPTY -eq 1 ]]; then
  rc=0
  workspace_is_empty || rc=$?
  case $rc in
    0) ;;
    1) die 'The live workspace is not empty; refusing --replace-empty. Use the default restore directory or --force explicitly.' ;;
    *) die 'Could not verify that the live workspace is empty; nothing was changed.' ;;
  esac
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
REMOTE_PARENT=$(dirname "$COLAB_WORKSPACE")
if [[ $FORCE -eq 1 ]]; then
  warn "Merging the archive into the live workspace $COLAB_WORKSPACE; matching files will be overwritten."
  REMOTE_CMD="tar -xzf - -C $REMOTE_PARENT"
  TARGET=$COLAB_WORKSPACE
elif [[ $REPLACE_EMPTY -eq 1 ]]; then
  log "Restoring into the empty live workspace $COLAB_WORKSPACE."
  REMOTE_CMD="tar -xzf - -C $REMOTE_PARENT"
  TARGET=$COLAB_WORKSPACE
else
  TARGET="$REMOTE_PARENT/$REMOTE_BASE.restore-$STAMP"
  REMOTE_CMD="tar -xzf - -C $REMOTE_PARENT --transform 's,^$REMOTE_BASE,$REMOTE_BASE.restore-$STAMP,'"
fi

if ! ssh -T -F "$COLAB_SSH_CONFIG" -o BatchMode=yes -o ConnectTimeout=10 "$COLAB_HOST" "$REMOTE_CMD" < "$ARCHIVE"; then
  die 'Restore failed. The runtime was not stopped.'
fi

log "Restored the archive into $TARGET on $COLAB_SESSION."
if [[ $FORCE -eq 0 && $REPLACE_EMPTY -eq 0 ]]; then
  log 'Move or merge the restored directory when you are satisfied with it.'
fi
