#!/usr/bin/env bash
# shellcheck disable=SC2016
# Provision (once) and bootstrap a disposable Google Colab runtime.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

REBUILD=0
REQUESTED_SESSION=
REQUESTED_GPU=
REQUESTED_TPU=
REQUESTED_HIGH_MEM=0

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/up.sh [options]

Reuses the tracked runtime when it exists. Creates at most one CPU runtime when
none exists, then installs the Agent Sandbox tools inside it.

Options:
  -r, --rebuild       Re-run the remote bootstrap even when it is current
      --session NAME  Use this session name (persisted locally)
      --gpu GPU       Request T4, L4, G4, H100 or A100 for a new runtime
      --tpu TPU       Request v5e1 or v6e1 for a new runtime
      --high-mem      Request a high-RAM machine shape (plan dependent)
  -h, --help          Show this help
TEXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--rebuild) REBUILD=1 ;;
    --session)
      [[ $# -ge 2 && -n $2 ]] || die '--session requires a name.'
      validate_session_name "$2"
      REQUESTED_SESSION=$2
      shift
      ;;
    --gpu)
      [[ $# -ge 2 && -n $2 ]] || die '--gpu requires a value.'
      validate_gpu "$2"
      REQUESTED_GPU=$(printf '%s' "$2" | tr '[:lower:]' '[:upper:]')
      shift
      ;;
    --tpu)
      [[ $# -ge 2 && -n $2 ]] || die '--tpu requires a value.'
      validate_tpu "$2"
      REQUESTED_TPU=$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')
      shift
      ;;
    --high-mem) REQUESTED_HIGH_MEM=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1. Use --help." ;;
  esac
  shift
done

[[ -z $REQUESTED_GPU || -z $REQUESTED_TPU ]] || die 'Use either --gpu or --tpu, not both.'

load_config
acquire_lock
trap 'release_lock' EXIT

check_auth_and_show_usage || die 'Google authentication check failed. Run `colab usage` once in a terminal and complete the sign-in flow.'

if [[ -n $REQUESTED_SESSION && $REQUESTED_SESSION != "$COLAB_SESSION" ]]; then
  OLD_SESSION=$COLAB_SESSION
  rc=0
  session_exists || rc=$?
  case $rc in
    0)
      die "Runtime '$OLD_SESSION' is still active. Run ./scripts/colab/down.sh before switching to '$REQUESTED_SESSION'."
      ;;
    3)
      die "The runtime tracked as '$OLD_SESSION' (endpoint '$COLAB_ENDPOINT') is still active but its local entry is gone. Resolve it with 'colab sessions' before switching sessions."
      ;;
    2)
      die 'Could not verify whether the current session is active. No runtime was created.'
      ;;
    1) ;;
    *) die 'Unexpected session lookup result. No runtime was created.' ;;
  esac
  log "Switching the local Colab session name from '$OLD_SESSION' to '$REQUESTED_SESSION'."
  config_set COLAB_SESSION "$REQUESTED_SESSION"
  clear_runtime_metadata
  load_config
fi

STATUS_LINE=
HARDWARE=
SHAPE=
rc=0
session_exists || rc=$?
case $rc in
  0)
    log "Reusing the existing Colab runtime '$COLAB_SESSION'."
    STATUS_LINE=$(session_status_line) || die 'The runtime is listed but its status could not be read. No runtime was created.'
    HARDWARE=$(status_field "$STATUS_LINE" Hardware || true)
    SHAPE=$(status_field "$STATUS_LINE" Shape || true)
    record_runtime_metadata "$STATUS_LINE"
    if [[ -n $REQUESTED_GPU ]]; then
      [[ $HARDWARE == "$REQUESTED_GPU" ]] \
        || die "The active runtime uses '$HARDWARE', not '$REQUESTED_GPU'. Run ./scripts/colab/down.sh first to change hardware."
    fi
    if [[ -n $REQUESTED_TPU ]]; then
      CURRENT_TPU=$(printf '%s' "$HARDWARE" | tr '[:upper:]' '[:lower:]')
      [[ $CURRENT_TPU == "$REQUESTED_TPU" ]] \
        || die "The active runtime uses '$HARDWARE', not '$REQUESTED_TPU'. Run ./scripts/colab/down.sh first to change hardware."
    fi
    if [[ $REQUESTED_HIGH_MEM -eq 1 && $SHAPE != High-RAM ]]; then
      die "The active runtime uses the '$SHAPE' shape. Run ./scripts/colab/down.sh first to request --high-mem."
    fi
    ;;
  1)
    log "No runtime named '$COLAB_SESSION' is active; provisioning one runtime."
    NEW_ARGS=(-s "$COLAB_SESSION")
    [[ -n $REQUESTED_GPU ]] && NEW_ARGS+=(--gpu "$REQUESTED_GPU")
    [[ -n $REQUESTED_TPU ]] && NEW_ARGS+=(--tpu "$REQUESTED_TPU")
    [[ $REQUESTED_HIGH_MEM -eq 1 ]] && NEW_ARGS+=(--high-mem)
    if ! run_colab new "${NEW_ARGS[@]}"; then
      die 'Colab refused the runtime allocation. No retry was attempted and no additional runtime was created. Check `colab usage`, your account limits, and try again.'
    fi
    rc=0
    session_exists || rc=$?
    case $rc in
      0) ;;
      1) die 'Colab reported a successful allocation, but the new session is not listed. Inspect `colab sessions`; no second runtime will be created automatically.' ;;
      3) die "Colab allocated endpoint '$COLAB_ENDPOINT', but its local session entry is inconsistent. Inspect 'colab sessions'; no second runtime will be created." ;;
      *) die 'Colab reported a successful allocation, but the session lookup failed. Inspect `colab sessions`; no second runtime will be created.' ;;
    esac
    STATUS_LINE=$(session_status_line) || die 'The new runtime is listed but its status could not be read.'
    HARDWARE=$(status_field "$STATUS_LINE" Hardware || true)
    SHAPE=$(status_field "$STATUS_LINE" Shape || true)
    record_runtime_metadata "$STATUS_LINE"
    ;;
  3)
    die "The runtime for recorded endpoint '$COLAB_ENDPOINT' is still active, but its local session entry is gone. Inspect 'colab sessions' and stop it explicitly. No runtime was created."
    ;;
  *)
    die 'Could not query Colab sessions. No runtime was created.'
    ;;
esac

write_ssh_config
if ! wait_for_ssh; then
  die "The runtime '$COLAB_SESSION' is active, but SSH did not become reachable. The runtime was left running. Re-run ./scripts/colab/up.sh to resume, or ./scripts/colab/down.sh to release it. No additional runtime was created."
fi

PAYLOAD_HASH=$(payload_hash)
REMOTE_VERSION=$(remote_marker || true)

if [[ $REBUILD -eq 0 && $REMOTE_VERSION == "$PAYLOAD_HASH" ]]; then
  log 'Remote bootstrap is already current; skipping reinstallation.'
else
  if [[ $REBUILD -eq 1 ]]; then
    log 'Forcing a remote bootstrap refresh.'
  else
    log 'Bootstrapping the Colab runtime (first run can take several minutes)...'
  fi
  bootstrap_runtime "$REBUILD" "$PAYLOAD_HASH" \
    || die "Bootstrap failed. The runtime '$COLAB_SESSION' remains active; re-run ./scripts/colab/up.sh to resume, or ./scripts/colab/down.sh to release it. No additional runtime was created."
  if [[ "$(remote_marker || true)" != "$PAYLOAD_HASH" ]]; then
    die 'Bootstrap finished without recording a complete marker. Re-run ./scripts/colab/up.sh to resume; no additional runtime was created.'
  fi
fi

release_lock
trap - EXIT

cat <<TEXT

Agent Sandbox on Google Colab is ready.
  Session:   $COLAB_SESSION
  Hardware:  ${HARDWARE:-unknown}${SHAPE:+ ($SHAPE shape)}
  Workspace: $COLAB_WORKSPACE

Connect:   ./scripts/colab/connect.sh
Status:    ./scripts/colab/status.sh
Save work: ./scripts/colab/save.sh
Release:   ./scripts/colab/down.sh

The runtime is disposable: files outside the workspace and local archives disappear when it is released or expires.
TEXT
