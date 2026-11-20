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
NO_CACHE=0
FORCE_CACHE=0
REFRESH_CACHE=0
RESTORE_WORKSPACE=0
WORKSPACE_ARCHIVE=
BENCHMARK=0

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/up.sh [options]

Reuses the tracked runtime when it exists. Creates at most one CPU runtime when
none exists, then installs the Agent Sandbox tools inside it. Independent
installs run concurrently. A compatible local environment cache is restored
only when the cache is enabled (default: off; see --cache).

Options:
  -r, --rebuild       Re-run and refresh the remote bootstrap and cache
      --no-cache      Do not restore or capture the environment cache
      --cache         Enable and restore/capture the environment cache
      --refresh-cache Recapture cache artifacts from the active runtime
      --restore-workspace[=ARCHIVE]
                      Restore the newest saved workspace archive, or ARCHIVE,
                      after the environment is ready. Only an empty workspace
                      is replaced; otherwise a new directory is used.
      --workspace-archive ARCHIVE
                      Same as --restore-workspace=ARCHIVE
      --benchmark     Print network and phase measurements
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
    --no-cache) NO_CACHE=1 ;;
    --cache) FORCE_CACHE=1 ;;
    --refresh-cache) REFRESH_CACHE=1 ;;
    --restore-workspace)
      RESTORE_WORKSPACE=1
      ;;
    --restore-workspace=*)
      RESTORE_WORKSPACE=1
      WORKSPACE_ARCHIVE=${1#--restore-workspace=}
      ;;
    --workspace-archive)
      [[ $# -ge 2 && -n $2 ]] || die '--workspace-archive requires a path.'
      RESTORE_WORKSPACE=1
      WORKSPACE_ARCHIVE=$2
      shift
      ;;
    --benchmark) BENCHMARK=1 ;;
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
[[ $NO_CACHE -eq 0 || $REFRESH_CACHE -eq 0 ]] || die 'Use either --no-cache or --refresh-cache, not both.'

load_config
[[ $NO_CACHE -eq 1 ]] && COLAB_CACHE=0
[[ $FORCE_CACHE -eq 1 || $REFRESH_CACHE -eq 1 ]] && COLAB_CACHE=1
acquire_lock
trap 'release_lock' EXIT

RUN_START=$(date +%s)
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
  [[ $NO_CACHE -eq 1 ]] && COLAB_CACHE=0
  [[ $FORCE_CACHE -eq 1 || $REFRESH_CACHE -eq 1 ]] && COLAB_CACHE=1
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
SSH_DONE=$(date +%s)

PAYLOAD_HASH=$(payload_hash)
REMOTE_VERSION=$(remote_marker || true)
MARKER_BEFORE=$REMOTE_VERSION
NEED_BOOTSTRAP=0
if [[ $REBUILD -eq 1 || $REMOTE_VERSION != "$PAYLOAD_HASH" ]]; then
  NEED_BOOTSTRAP=1
fi
NEED_CACHE_STAGE=0
if [[ $COLAB_CACHE == 1 ]] && { [[ $NEED_BOOTSTRAP -eq 1 ]] || [[ $REFRESH_CACHE -eq 1 ]]; }; then
  NEED_CACHE_STAGE=1
fi

CACHE_PROFILE=
CACHE_RESTORED_NVIM=0
CACHE_RESTORED_SHELL=0
CACHE_RESTORED_NVIM_META=
CACHE_RESTORED_SHELL_META=
CACHE_RESTORE_SECONDS=0
CACHE_CAPTURE_SECONDS=0
BOOTSTRAP_SECONDS=0
CACHE_CAPTURE_NVIM=0
CACHE_CAPTURE_SHELL=0
STAGING=
NET_RX_START=
NET_TX_START=

cleanup_staging() {
  if [[ -n ${STAGING:-} ]]; then
    rm -rf -- "$STAGING"
    STAGING=
  fi
}
trap 'cleanup_staging; release_lock' EXIT

if [[ $BENCHMARK -eq 1 ]]; then
  NET_RX_START=$(remote_network_bytes rx || printf 0)
  NET_TX_START=$(remote_network_bytes tx || printf 0)
fi

if [[ $NEED_BOOTSTRAP -eq 1 ]]; then
  if [[ $REBUILD -eq 1 ]]; then
    log 'Forcing a remote bootstrap refresh.'
  else
    log 'Bootstrapping the Colab runtime (first run can take several minutes)...'
  fi
  STAGING=$(make_payload)
  if ! stage_payload "$STAGING"; then
    die "Could not upload the bootstrap payload. The runtime '$COLAB_SESSION' remains active; re-run ./scripts/colab/up.sh to resume, or ./scripts/colab/down.sh to release it. No additional runtime was created."
  fi

  if [[ $NEED_CACHE_STAGE -eq 1 ]]; then
    CACHE_PROFILE=$(cache_remote_profile "$STAGING" || true)
    if [[ -n $CACHE_PROFILE ]]; then
      CACHE_PROFILE=$(cache_profile_id "$CACHE_PROFILE")
      log "Environment cache profile: $CACHE_PROFILE"
      # Restore only on a runtime without a recorded bootstrap. A stale marker
      # means tools are already installed, so overlaying cache paths would
      # waste transfer and risk replacing a working environment.
      if [[ -z $MARKER_BEFORE ]]; then
        local_lock=$(cache_local_lazy_lock)
        restore_start=$(date +%s)
        if cache_component_enabled nvim-plugins && cache_restore_component "$STAGING" "$CACHE_PROFILE" nvim-plugins "$local_lock"; then
          CACHE_RESTORED_NVIM=1
          CACHE_RESTORED_NVIM_META=$CACHE_RESTORED_META
        fi
        if cache_component_enabled shell && cache_restore_component "$STAGING" "$CACHE_PROFILE" shell; then
          CACHE_RESTORED_SHELL=1
          CACHE_RESTORED_SHELL_META=$CACHE_RESTORED_META
        fi
        CACHE_RESTORE_SECONDS=$(( $(date +%s) - restore_start ))
      fi
    else
      warn 'Could not determine the remote cache profile; running the normal bootstrap.'
    fi
  fi

  bootstrap_start=$(date +%s)
  bootstrap_rc=0
  run_bootstrap "$REBUILD" "$PAYLOAD_HASH" || bootstrap_rc=$?
  BOOTSTRAP_SECONDS=$(( $(date +%s) - bootstrap_start ))
  if [[ $bootstrap_rc -ne 0 ]]; then
    if [[ -n $CACHE_RESTORED_NVIM_META ]]; then
      cache_discard_meta "$CACHE_RESTORED_NVIM_META"
    fi
    if [[ -n $CACHE_RESTORED_SHELL_META ]]; then
      cache_discard_meta "$CACHE_RESTORED_SHELL_META"
    fi
    die "Bootstrap failed. The runtime '$COLAB_SESSION' remains active; re-run ./scripts/colab/up.sh to resume, or ./scripts/colab/down.sh to release it. No additional runtime was created."
  fi
  if [[ "$(remote_marker || true)" != "$PAYLOAD_HASH" ]]; then
    die 'Bootstrap finished without recording a complete marker. Re-run ./scripts/colab/up.sh to resume; no additional runtime was created.'
  fi

  if [[ $COLAB_CACHE == 1 ]]; then
    # Capture only what was not already restored, plus a requested refresh.
    if cache_component_enabled nvim-plugins && [[ $CACHE_RESTORED_NVIM -eq 0 || $REBUILD -eq 1 || $REFRESH_CACHE -eq 1 ]]; then
      CACHE_CAPTURE_NVIM=1
    fi
    if cache_component_enabled shell && [[ $CACHE_RESTORED_SHELL -eq 0 || $REBUILD -eq 1 || $REFRESH_CACHE -eq 1 ]]; then
      CACHE_CAPTURE_SHELL=1
    fi
  fi
elif [[ $REFRESH_CACHE -eq 1 ]]; then
  log 'Recapturing the environment cache from the active runtime.'
  STAGING=$(make_payload)
  stage_payload "$STAGING" || die 'Could not upload the cache helper. The runtime was not changed.'
  CACHE_PROFILE=$(cache_remote_profile "$STAGING" || true)
  if [[ -z $CACHE_PROFILE ]]; then
    die 'Could not determine the remote cache profile.'
  fi
  CACHE_PROFILE=$(cache_profile_id "$CACHE_PROFILE")
  if cache_component_enabled nvim-plugins; then CACHE_CAPTURE_NVIM=1; fi
  if cache_component_enabled shell; then CACHE_CAPTURE_SHELL=1; fi
  [[ $CACHE_CAPTURE_NVIM -eq 1 || $CACHE_CAPTURE_SHELL -eq 1 ]] \
    || die 'No cache components are enabled (see COLAB_CACHE_COMPONENTS).'
else
  log 'Remote bootstrap is already current; skipping reinstallation.'
fi

if [[ $COLAB_CACHE == 1 && ( $CACHE_CAPTURE_NVIM -eq 1 || $CACHE_CAPTURE_SHELL -eq 1 ) ]]; then
  [[ -n $CACHE_PROFILE ]] || CACHE_PROFILE=$(cache_remote_profile "$STAGING" || true)
  if [[ -n $CACHE_PROFILE ]]; then
    capture_start=$(date +%s)
    if [[ $CACHE_CAPTURE_NVIM -eq 1 ]]; then
      cache_capture_nvim_plugins "$STAGING" "$CACHE_PROFILE" || true
    fi
    if [[ $CACHE_CAPTURE_SHELL -eq 1 ]]; then
      cache_capture_shell "$STAGING" "$CACHE_PROFILE" || true
    fi
    CACHE_CAPTURE_SECONDS=$(( $(date +%s) - capture_start ))
  fi
fi

if [[ $RESTORE_WORKSPACE -eq 1 ]]; then
  if [[ -z $WORKSPACE_ARCHIVE ]]; then
    WORKSPACE_ARCHIVE=$(find "$COLAB_REPO_ROOT/backups" -maxdepth 1 -type f -name 'colab-workspace-*.tar.gz' -print 2>/dev/null \
      | LC_ALL=C sort | tail -n 1)
    [[ -n $WORKSPACE_ARCHIVE ]] || die 'No workspace archive found under backups/. Pass --workspace-archive PATH.'
  fi
  [[ -f $WORKSPACE_ARCHIVE ]] || die "Workspace archive is not readable: $WORKSPACE_ARCHIVE"
  rc=0
  workspace_is_empty || rc=$?
  case $rc in
    0)
      log "Restoring $WORKSPACE_ARCHIVE into the empty remote workspace."
      "$SCRIPT_DIR/restore.sh" --replace-empty "$WORKSPACE_ARCHIVE" \
        || die 'Workspace restore failed. The environment is ready; the runtime was left running.'
      ;;
    1)
      warn 'The remote workspace is not empty; restoring into a new directory instead of overwriting it.'
      "$SCRIPT_DIR/restore.sh" "$WORKSPACE_ARCHIVE" \
        || die 'Workspace restore failed. The environment is ready; the runtime was left running.'
      ;;
    *)
      die 'Could not determine whether the remote workspace is empty; no workspace files were changed.'
      ;;
  esac
fi

cleanup_staging
release_lock
trap - EXIT

TOTAL_SECONDS=$(( $(date +%s) - RUN_START ))
CACHE_STATE_TEXT='disabled'
if [[ $COLAB_CACHE == 1 ]]; then
  CACHE_STATE_TEXT=$(cache_summary)
fi

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

printf '\nTimings:\n'
printf '  Runtime and SSH:  %ss\n' "$((SSH_DONE - RUN_START))"
printf '  Cache restore:    %ss\n' "$CACHE_RESTORE_SECONDS"
printf '  Bootstrap:        %ss\n' "$BOOTSTRAP_SECONDS"
printf '  Cache capture:    %ss\n' "$CACHE_CAPTURE_SECONDS"
printf '  Total:            %ss\n' "$TOTAL_SECONDS"
printf '%s\n' "$CACHE_STATE_TEXT"

if [[ $BENCHMARK -eq 1 ]]; then
  NET_RX_END=$(remote_network_bytes rx || printf 0)
  NET_TX_END=$(remote_network_bytes tx || printf 0)
  printf '\nNetwork (remote interface counters, includes protocol overhead):\n'
  printf '  Received: %s\n' "$(human_bytes "$((NET_RX_END - NET_RX_START))")"
  printf '  Sent:     %s\n' "$(human_bytes "$((NET_TX_END - NET_TX_START))")"
fi
