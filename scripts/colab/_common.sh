#!/usr/bin/env bash
# Shared helpers for the Google Colab backend. This file is sourced, not executed.
# It intentionally avoids bash 4-only features so a stock macOS bash can use it.

COLAB_SCRIPTS_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COLAB_REPO_ROOT=$(cd -- "$COLAB_SCRIPTS_DIR/../.." && pwd)
COLAB_DIR="$COLAB_REPO_ROOT/.colab"
COLAB_CONFIG="$COLAB_DIR/config.env"
COLAB_SSH_DIR="$COLAB_DIR/ssh"
COLAB_KEY_DEFAULT="$COLAB_SSH_DIR/id_ed25519"
COLAB_SSH_CONFIG="$COLAB_SSH_DIR/config"
COLAB_GITCONFIG="$COLAB_DIR/gitconfig"
COLAB_LOCK_DIR="$COLAB_DIR/lock.d"

COLAB_HOST="agent-sandbox-colab"
COLAB_FORCE_HOST="agent-sandbox-colab-force"
COLAB_STATE_DIR="/root/.agent-sandbox"
COLAB_REMOTE_MARKER="$COLAB_STATE_DIR/bootstrap-version"

log() {
  if [[ ${AGENT_COLAB_STDERR:-0} == 1 ]]; then
    printf '%s\n' "$*" >&2
  else
    printf '%s\n' "$*"
  fi
}

warn() { printf 'WARN: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256
  else
    die 'Missing sha256sum or shasum.'
  fi
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$1"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- "$1"
  else
    die 'Missing sha256sum or shasum.'
  fi
}

# Quote one argument for a POSIX shell (used in generated ProxyCommand lines).
shell_quote() {
  local value=$1
  printf "'%s'" "${value//\'/\'\\\'\'}"
}

validate_session_name() {
  [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$ ]] \
    || die "Invalid Colab session name '$1'. Use letters, digits, '-' or '_' (max 63 chars)."
}

validate_gpu() {
  case "$1" in
    T4|t4|L4|l4|G4|g4|H100|h100|A100|a100) ;;
    *) die "Unsupported GPU '$1'. Supported: T4, L4, G4, H100, A100." ;;
  esac
}

validate_tpu() {
  case "$1" in
    v5e1|V5E1|v6e1|V6E1) ;;
    *) die "Unsupported TPU '$1'. Supported: v5e1, v6e1." ;;
  esac
}

validate_positive_int() {
  [[ $1 =~ ^[1-9][0-9]*$ ]] || die "$2 must be a positive integer."
}

validate_nonnegative_int() {
  [[ $1 =~ ^[0-9]+$ ]] || die "$2 must be a non-negative integer."
}

config_set() {
  local key=$1
  local value=$2
  local tmp
  [[ -f $COLAB_CONFIG ]] || die 'Colab configuration is missing. Run ./scripts/colab/setup.sh first.'
  tmp=$(mktemp "${COLAB_CONFIG}.tmp.XXXXXX") || die 'Could not create a temporary configuration file.'
  {
    local found=0
    local line
    while IFS= read -r line || [[ -n $line ]]; do
      if [[ $line == "$key="* ]]; then
        printf '%s=%q\n' "$key" "$value"
        found=1
      else
        printf '%s\n' "$line"
      fi
    done < "$COLAB_CONFIG"
    if [[ $found -eq 0 ]]; then
      printf '%s=%q\n' "$key" "$value"
    fi
  } > "$tmp"
  chmod 0600 "$tmp"
  mv -f -- "$tmp" "$COLAB_CONFIG"
}

load_config() {
  [[ -f $COLAB_CONFIG ]] || die 'Colab backend is not configured. Run ./scripts/colab/setup.sh first.'
  # shellcheck disable=SC1090
  source "$COLAB_CONFIG"

  : "${COLAB_SESSION:=agent-sandbox}"
  : "${COLAB_AUTH:=oauth2}"
  : "${COLAB_STATE_FILE:=}"
  : "${COLAB_SSH_KEY:=$COLAB_KEY_DEFAULT}"
  : "${COLAB_WORKSPACE:=/home/agent/workspace}"
  : "${COLAB_ENDPOINT:=}"
  : "${COLAB_ACCELERATOR:=}"
  : "${COLAB_TPU:=}"
  : "${COLAB_HIGH_MEM:=0}"
  : "${COLAB_CODEX_VERSION:=latest}"
  : "${COLAB_CLAUDE_VERSION:=latest}"
  : "${COLAB_OPENCODE_VERSION:=latest}"
  : "${COLAB_AGY_INSTALL:=1}"
  : "${COLAB_SSH_WAIT_ATTEMPTS:=30}"
  : "${COLAB_SSH_WAIT_SECONDS:=2}"
  : "${COLAB_CLI:=}"

  validate_session_name "$COLAB_SESSION"
  case "$COLAB_AUTH" in
    oauth2|adc) ;;
    *) die "COLAB_AUTH must be 'oauth2' or 'adc'." ;;
  esac
  case "$COLAB_HIGH_MEM" in
    0|1) ;;
    *) die 'COLAB_HIGH_MEM must be 0 or 1.' ;;
  esac
  case "$COLAB_AGY_INSTALL" in
    0|1) ;;
    *) die 'COLAB_AGY_INSTALL must be 0 or 1.' ;;
  esac
  validate_positive_int "$COLAB_SSH_WAIT_ATTEMPTS" COLAB_SSH_WAIT_ATTEMPTS
  validate_nonnegative_int "$COLAB_SSH_WAIT_SECONDS" COLAB_SSH_WAIT_SECONDS
  [[ -z $COLAB_ENDPOINT || $COLAB_ENDPOINT =~ ^[A-Za-z0-9._-]+$ ]] || die 'COLAB_ENDPOINT has an invalid value.'
  [[ -z $COLAB_ACCELERATOR || $COLAB_ACCELERATOR =~ ^[A-Za-z0-9]+$ ]] || die 'COLAB_ACCELERATOR has an invalid value.'
  [[ -z $COLAB_TPU || $COLAB_TPU =~ ^[A-Za-z0-9]+$ ]] || die 'COLAB_TPU has an invalid value.'
  for value in "$COLAB_CODEX_VERSION" "$COLAB_CLAUDE_VERSION" "$COLAB_OPENCODE_VERSION"; do
    [[ $value =~ ^[A-Za-z0-9._@/-]+$ ]] || die 'Agent version values may only contain letters, digits, dot, dash, slash, underscore or @.'
  done
  [[ $COLAB_WORKSPACE =~ ^/[A-Za-z0-9._/-]+$ ]] || die 'COLAB_WORKSPACE must be an absolute path without spaces or shell metacharacters.'
  [[ $COLAB_WORKSPACE != *..* ]] || die 'COLAB_WORKSPACE must not contain "..".'

  if [[ $COLAB_SSH_KEY != /* ]]; then
    COLAB_SSH_KEY="$COLAB_REPO_ROOT/$COLAB_SSH_KEY"
  fi
  if [[ -n $COLAB_STATE_FILE && $COLAB_STATE_FILE != /* ]]; then
    COLAB_STATE_FILE="$COLAB_REPO_ROOT/$COLAB_STATE_FILE"
  fi

  [[ -n $COLAB_CLI ]] || die 'COLAB_CLI is not configured. Run ./scripts/colab/setup.sh first.'
  if [[ $COLAB_CLI == */* ]]; then
    [[ -x $COLAB_CLI ]] || die "Colab CLI is not executable: $COLAB_CLI"
    COLAB_CLI_ABS=$COLAB_CLI
  else
    COLAB_CLI_ABS=$(command -v "$COLAB_CLI" 2>/dev/null || true)
    [[ -n $COLAB_CLI_ABS ]] || die "Colab CLI '$COLAB_CLI' was not found on PATH."
  fi
}

build_colab_argv() {
  COLAB_ARGV=("$COLAB_CLI_ABS")
  [[ -n ${COLAB_AUTH:-} ]] && COLAB_ARGV+=(--auth "$COLAB_AUTH")
  [[ -n ${COLAB_STATE_FILE:-} ]] && COLAB_ARGV+=(--config "$COLAB_STATE_FILE")
}

run_colab() {
  build_colab_argv
  "${COLAB_ARGV[@]}" "$@"
}

check_auth_and_show_usage() {
  local output rc=0
  if [[ ${AGENT_COLAB_NONINTERACTIVE:-0} == 1 ]]; then
    output=$(run_colab usage </dev/null 2>&1) || rc=$?
  else
    output=$(run_colab usage 2>&1) || rc=$?
  fi
  if [[ $rc -ne 0 ]]; then
    printf '%s\n' "$output" >&2
    return 1
  fi
  printf '%s\n' "$output"
}

# 0 = tracked session exists, 1 = absent, 2 = backend lookup failed,
# 3 = the recorded endpoint is still active but its local name is gone.
session_exists() {
  local output rc=0
  if [[ ${AGENT_COLAB_NONINTERACTIVE:-0} == 1 ]]; then
    output=$(run_colab sessions </dev/null 2>&1) || rc=$?
  else
    output=$(run_colab sessions 2>&1) || rc=$?
  fi
  if [[ $rc -ne 0 ]]; then
    printf '%s\n' "$output" >&2
    return 2
  fi
  if [[ $output == *"[$COLAB_SESSION] "* ]]; then
    return 0
  fi
  if [[ -n ${COLAB_ENDPOINT:-} && $output == *"$COLAB_ENDPOINT"* ]]; then
    printf '%s\n' "$output" >&2
    return 3
  fi
  return 1
}

require_existing_session() {
  local rc=0
  session_exists || rc=$?
  case $rc in
    0) ;;
    1) die "No active Colab runtime named '$COLAB_SESSION'. Run ./scripts/colab/up.sh first." ;;
    3) die "The runtime for recorded endpoint '$COLAB_ENDPOINT' is still active, but its local session entry is gone. Inspect 'colab sessions' and stop it explicitly before creating another runtime." ;;
    *) die 'Could not query Colab sessions. Fix the API/authentication error above; no runtime was created.' ;;
  esac
}

session_status_line() {
  local output rc=0
  output=$(run_colab status -s "$COLAB_SESSION" 2>&1) || rc=$?
  if [[ $rc -ne 0 ]]; then
    printf '%s\n' "$output" >&2
    return 2
  fi
  local line
  line=$(printf '%s\n' "$output" | grep -F -- "[$COLAB_SESSION] " | head -n 1 || true)
  [[ -n $line ]] || return 1
  printf '%s\n' "$line"
}

status_endpoint() {
  local line=$1
  line=${line#*]}
  line=${line# }
  printf '%s\n' "${line%% | *}"
}

status_field() {
  local line=$1
  local key=$2
  local part
  local old_ifs=$IFS
  local glob_state=$-
  IFS='|'
  set -f
  # Deliberate splitting on '|' with globbing disabled; fields may contain spaces.
  # shellcheck disable=SC2086
  set -- $line
  IFS=$old_ifs
  [[ $glob_state == *f* ]] || set +f
  for part in "$@"; do
    part=${part# }
    part=${part% }
    if [[ $part == "$key: "* ]]; then
      printf '%s\n' "${part#*: }"
      return 0
    fi
  done
  return 1
}

record_runtime_metadata() {
  local line=$1
  local endpoint hardware shape accel tpu high_mem
  endpoint=$(status_endpoint "$line")
  hardware=$(status_field "$line" Hardware || true)
  shape=$(status_field "$line" Shape || true)
  accel=
  tpu=
  case "$hardware" in
    CPU) ;;
    T4|L4|G4|H100|A100) accel=$hardware ;;
    V5E1|V6E1)
      tpu=$(printf '%s' "$hardware" | tr '[:upper:]' '[:lower:]')
      ;;
    *) warn "Unrecognized hardware label '$hardware'." ;;
  esac
  high_mem=0
  [[ $shape == High-RAM ]] && high_mem=1
  config_set COLAB_ENDPOINT "$endpoint"
  config_set COLAB_ACCELERATOR "$accel"
  config_set COLAB_TPU "$tpu"
  config_set COLAB_HIGH_MEM "$high_mem"
  COLAB_ENDPOINT=$endpoint
  COLAB_ACCELERATOR=$accel
  COLAB_TPU=$tpu
  COLAB_HIGH_MEM=$high_mem
}

clear_runtime_metadata() {
  config_set COLAB_ENDPOINT ''
  config_set COLAB_ACCELERATOR ''
  config_set COLAB_TPU ''
  config_set COLAB_HIGH_MEM '0'
  COLAB_ENDPOINT=
  COLAB_ACCELERATOR=
  COLAB_TPU=
  COLAB_HIGH_MEM=0
}

proxy_command_string() {
  local force=$1
  build_colab_argv
  local args=("${COLAB_ARGV[@]}" ssh --proxy-mode -s "$COLAB_SESSION" --identity "$COLAB_SSH_KEY")
  [[ $force -eq 1 ]] && args+=(--force)
  local out=
  local arg
  for arg in "${args[@]}"; do
    out+="$(shell_quote "$arg") "
  done
  printf '%s' "${out% }"
}

write_ssh_config() {
  [[ -f $COLAB_SSH_KEY && -r $COLAB_SSH_KEY ]] \
    || die "SSH key is missing or unreadable: $COLAB_SSH_KEY. Run ./scripts/colab/setup.sh."
  mkdir -p "$COLAB_SSH_DIR"
  chmod 0700 "$COLAB_DIR" "$COLAB_SSH_DIR" 2>/dev/null || true
  local tmp
  tmp=$(mktemp "${COLAB_SSH_CONFIG}.tmp.XXXXXX") || die 'Could not create SSH config.'
  {
    printf '%s\n' '# Generated by scripts/colab/setup.sh. Do not edit; re-run setup/up to refresh.'
    printf '%s\n' "Host $COLAB_HOST"
    printf '    HostName %s\n' 'colab-runtime'
    printf '%s\n' '    User root'
    printf '    IdentityFile "%s"\n' "$COLAB_SSH_KEY"
    printf '%s\n' '    IdentitiesOnly yes'
    printf '%s\n' '    PreferredAuthentications publickey'
    printf '    ProxyCommand %s\n' "$(proxy_command_string 0)"
    printf '%s\n' '    StrictHostKeyChecking no'
    printf '%s\n' '    UserKnownHostsFile /dev/null'
    printf '%s\n' '    GlobalKnownHostsFile /dev/null'
    printf '%s\n' '    LogLevel ERROR'
    printf '%s\n' '    ConnectTimeout 10'
    printf '%s\n' '    ForwardAgent no'
    printf '%s\n' '    ForwardX11 no'
    printf '\n'
    printf '%s\n' "Host $COLAB_FORCE_HOST"
    printf '    HostName %s\n' 'colab-runtime'
    printf '%s\n' '    User root'
    printf '    IdentityFile "%s"\n' "$COLAB_SSH_KEY"
    printf '%s\n' '    IdentitiesOnly yes'
    printf '%s\n' '    PreferredAuthentications publickey'
    printf '    ProxyCommand %s\n' "$(proxy_command_string 1)"
    printf '%s\n' '    StrictHostKeyChecking no'
    printf '%s\n' '    UserKnownHostsFile /dev/null'
    printf '%s\n' '    GlobalKnownHostsFile /dev/null'
    printf '%s\n' '    LogLevel ERROR'
    printf '%s\n' '    ConnectTimeout 10'
    printf '%s\n' '    ForwardAgent no'
    printf '%s\n' '    ForwardX11 no'
  } > "$tmp"
  chmod 0600 "$tmp"
  mv -f -- "$tmp" "$COLAB_SSH_CONFIG"
}

ssh_run() {
  ssh -T -F "$COLAB_SSH_CONFIG" -o BatchMode=yes -o ConnectTimeout=10 "$COLAB_HOST" "$@"
}

wait_for_ssh() {
  local attempt=1
  while [[ $attempt -le $COLAB_SSH_WAIT_ATTEMPTS ]]; do
    if ssh_run 'printf ready' >/dev/null 2>&1; then
      return 0
    fi
    if [[ $attempt -lt $COLAB_SSH_WAIT_ATTEMPTS ]]; then
      sleep "$COLAB_SSH_WAIT_SECONDS"
    fi
    attempt=$((attempt + 1))
  done
  return 1
}

acquire_lock() {
  mkdir -p "$COLAB_DIR"
  chmod 0700 "$COLAB_DIR" 2>/dev/null || true
  if mkdir "$COLAB_LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$COLAB_LOCK_DIR/pid"
    return 0
  fi
  local pid=
  [[ -r $COLAB_LOCK_DIR/pid ]] && pid=$(cat "$COLAB_LOCK_DIR/pid" 2>/dev/null || true)
  if [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null; then
    die "Another Colab operation is already running (pid $pid)."
  fi
  warn 'Removing a stale Colab operation lock.'
  rm -rf -- "$COLAB_LOCK_DIR"
  mkdir "$COLAB_LOCK_DIR" || die 'Could not acquire the Colab operation lock.'
  printf '%s\n' "$$" > "$COLAB_LOCK_DIR/pid"
}

release_lock() {
  rm -rf -- "$COLAB_LOCK_DIR"
}

payload_hash() {
  (
    set -e
    cd "$COLAB_REPO_ROOT" || exit 1
    {
      printf '%s\n' 'scripts/colab/bootstrap.sh'
      printf '%s\n' 'scripts/agent-notify.sh'
      find config -type f -print
      if [[ -f $COLAB_GITCONFIG ]]; then
        printf '%s\n' '.colab/gitconfig'
      fi
    } | LC_ALL=C sort -u | while IFS= read -r file; do
      [[ -f $file ]] || continue
      printf '%s  %s\n' "$(sha256_file "$file" | awk '{print $1}')" "$file"
    done
    printf 'CODEX_VERSION=%s\nCLAUDE_VERSION=%s\nOPENCODE_VERSION=%s\nAGY_INSTALL=%s\n' \
      "$COLAB_CODEX_VERSION" "$COLAB_CLAUDE_VERSION" "$COLAB_OPENCODE_VERSION" "$COLAB_AGY_INSTALL"
  ) | sha256_stdin | awk '{print $1}'
}

make_payload() {
  local staging
  staging=$(mktemp -d "${TMPDIR:-/tmp}/agent-sandbox-colab.XXXXXX") || die 'Could not create a payload directory.'
  mkdir -p "$staging/payload"
  cp -R "$COLAB_REPO_ROOT/config" "$staging/payload/config"
  cp "$COLAB_REPO_ROOT/scripts/colab/bootstrap.sh" "$staging/payload/bootstrap.sh"
  cp "$COLAB_REPO_ROOT/scripts/agent-notify.sh" "$staging/payload/agent-notify.sh"
  if [[ -f $COLAB_GITCONFIG ]]; then
    cp "$COLAB_GITCONFIG" "$staging/payload/gitconfig"
  fi
  {
    printf 'CODEX_VERSION=%q\n' "$COLAB_CODEX_VERSION"
    printf 'CLAUDE_VERSION=%q\n' "$COLAB_CLAUDE_VERSION"
    printf 'OPENCODE_VERSION=%q\n' "$COLAB_OPENCODE_VERSION"
    printf 'AGY_INSTALL=%q\n' "$COLAB_AGY_INSTALL"
  } > "$staging/payload/env"
  chmod 0644 "$staging/payload/env" "$staging/payload/bootstrap.sh" "$staging/payload/agent-notify.sh"
  tar -C "$staging/payload" -czf "$staging/payload.tar.gz" .
  printf '%s\n' "$staging"
}

remote_marker() {
  ssh_run "cat $COLAB_REMOTE_MARKER 2>/dev/null || true" | tr -d '\r' | head -n 1
}

bootstrap_runtime() {
  local force=$1
  local version=$2
  local staging
  staging=$(make_payload)
  local rc=0
  if ! ssh_run "rm -rf /tmp/agent-sandbox-bootstrap && mkdir -p /tmp/agent-sandbox-bootstrap && tar -xzf - -C /tmp/agent-sandbox-bootstrap" < "$staging/payload.tar.gz"; then
    rm -rf -- "$staging"
    return 1
  fi
  local args_list="--version $version"
  [[ $force -eq 1 ]] && args_list="$args_list --force"
  ssh_run "bash /tmp/agent-sandbox-bootstrap/bootstrap.sh $args_list" || rc=$?
  if [[ $rc -ne 0 ]]; then
    printf '\n--- remote bootstrap log (tail) ---\n' >&2
    ssh_run "tail -n 80 $COLAB_STATE_DIR/bootstrap.log 2>/dev/null || true" >&2 || true
    printf '%s\n' '--- end remote bootstrap log ---' >&2
  fi
  rm -rf -- "$staging"
  return "$rc"
}

human_size() {
  if [[ -f $1 ]]; then
    if command -v du >/dev/null 2>&1; then
      du -h "$1" | awk '{print $1}'
    else
      printf '%s bytes' "$(wc -c < "$1" | tr -d ' ')"
    fi
  fi
}
