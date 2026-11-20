#!/usr/bin/env bash
# shellcheck disable=SC2016
# One-time local setup for the Google Colab backend.
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

FORCE_KEY=0
UPGRADE_CLI=0
REFRESH_GIT=0
SKIP_AUTH_CHECK=${COLAB_SKIP_AUTH_CHECK:-0}

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/setup.sh [options]

Creates private local state under .colab/ and installs the Colab CLI when needed.

Options:
  --rotate-key         Generate a new dedicated SSH key (the old one is kept as .bak)
  --upgrade-cli        Upgrade google-colab-cli when it was installed with uv/pipx
  --refresh-git-config Re-read user.name and user.email from the local Git config
  -h, --help           Show this help
TEXT
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rotate-key) FORCE_KEY=1 ;;
    --upgrade-cli) UPGRADE_CLI=1 ;;
    --refresh-git-config) REFRESH_GIT=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $1. Use --help." ;;
  esac
  shift
done

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) die 'The Google Colab CLI supports Linux and macOS clients only.' ;;
esac

for cmd in bash ssh ssh-keygen tar git curl; do
  require_command "$cmd"
done

install_colab_cli() {
  if [[ -n ${COLAB_CLI:-} && $COLAB_CLI == */* ]]; then
    [[ -x $COLAB_CLI ]] || die "COLAB_CLI is not executable: $COLAB_CLI"
    return 0
  fi
  if command -v colab >/dev/null 2>&1; then
    return 0
  fi
  log 'Colab CLI is not installed; installing google-colab-cli locally.'
  if command -v uv >/dev/null 2>&1; then
    if uv tool list 2>/dev/null | grep -q '^google-colab-cli'; then
      if [[ $UPGRADE_CLI -eq 1 ]]; then
        uv tool upgrade google-colab-cli
      fi
    else
      uv tool install google-colab-cli
    fi
  elif command -v pipx >/dev/null 2>&1; then
    if pipx list --short 2>/dev/null | grep -q '^google-colab-cli'; then
      if [[ $UPGRADE_CLI -eq 1 ]]; then
        pipx upgrade google-colab-cli
      fi
    else
      pipx install google-colab-cli
    fi
  else
    die 'Install google-colab-cli first, for example: uv tool install google-colab-cli'
  fi
  hash -r 2>/dev/null || true
  command -v colab >/dev/null 2>&1 || {
    [[ -x $HOME/.local/bin/colab ]] || die 'google-colab-cli was installed but colab is not on PATH. Add ~/.local/bin to PATH.'
  }
}

install_colab_cli

CLI_PATH=
if [[ -n ${COLAB_CLI:-} && $COLAB_CLI == */* ]]; then
  CLI_PATH=$COLAB_CLI
elif [[ -n ${COLAB_CLI:-} ]]; then
  CLI_PATH=$(command -v "$COLAB_CLI" 2>/dev/null || true)
fi
if [[ -z $CLI_PATH ]]; then
  CLI_PATH=$(command -v colab 2>/dev/null || true)
fi
[[ -n $CLI_PATH ]] || CLI_PATH="$HOME/.local/bin/colab"
[[ -x $CLI_PATH ]] || die 'Could not locate the colab executable.'
"$CLI_PATH" version >/dev/null 2>&1 || die "Colab CLI failed to run: $CLI_PATH version"
SSH_HELP=$("$CLI_PATH" ssh --help 2>&1 || true)
[[ $SSH_HELP == *--proxy-mode* ]] \
  || die 'This Colab CLI lacks `ssh --proxy-mode`. Upgrade with: uv tool upgrade google-colab-cli'

mkdir -p "$COLAB_DIR" "$COLAB_SSH_DIR"
chmod 0700 "$COLAB_DIR" "$COLAB_SSH_DIR"

acquire_lock
trap 'release_lock' EXIT

if [[ -f $COLAB_CONFIG ]]; then
  # shellcheck disable=SC1090
  source "$COLAB_CONFIG"
fi
: "${COLAB_SESSION:=agent-sandbox}"
: "${COLAB_AUTH:=oauth2}"
: "${COLAB_STATE_FILE:=}"
: "${COLAB_SSH_KEY:=.colab/ssh/id_ed25519}"
: "${COLAB_WORKSPACE:=/home/agent/workspace}"
: "${COLAB_ENDPOINT:=}"
: "${COLAB_ACCELERATOR:=}"
: "${COLAB_TPU:=}"
: "${COLAB_HIGH_MEM:=0}"
: "${COLAB_CODEX_VERSION:=}"
: "${COLAB_CLAUDE_VERSION:=}"
: "${COLAB_OPENCODE_VERSION:=}"
: "${COLAB_AGY_INSTALL:=1}"
: "${COLAB_SSH_WAIT_ATTEMPTS:=30}"
: "${COLAB_SSH_WAIT_SECONDS:=2}"
validate_session_name "$COLAB_SESSION"

if [[ -z $COLAB_CODEX_VERSION && -f $COLAB_REPO_ROOT/.env ]]; then
  COLAB_CODEX_VERSION=$(sed -n 's/^CODEX_VERSION=//p' "$COLAB_REPO_ROOT/.env" | tail -n 1)
fi
if [[ -z $COLAB_CLAUDE_VERSION && -f $COLAB_REPO_ROOT/.env ]]; then
  COLAB_CLAUDE_VERSION=$(sed -n 's/^CLAUDE_VERSION=//p' "$COLAB_REPO_ROOT/.env" | tail -n 1)
fi
if [[ -z $COLAB_OPENCODE_VERSION && -f $COLAB_REPO_ROOT/.env ]]; then
  COLAB_OPENCODE_VERSION=$(sed -n 's/^OPENCODE_VERSION=//p' "$COLAB_REPO_ROOT/.env" | tail -n 1)
fi
: "${COLAB_CODEX_VERSION:=latest}"
: "${COLAB_CLAUDE_VERSION:=latest}"
: "${COLAB_OPENCODE_VERSION:=latest}"

KEY_PATH=$COLAB_SSH_KEY
if [[ $KEY_PATH != /* ]]; then
  KEY_PATH="$COLAB_REPO_ROOT/$KEY_PATH"
fi
if [[ -f $KEY_PATH && $FORCE_KEY -eq 1 ]]; then
  backup="$KEY_PATH.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  mv -f -- "$KEY_PATH" "$backup"
  [[ -f "$KEY_PATH.pub" ]] && mv -f -- "$KEY_PATH.pub" "$backup.pub"
  log "Rotated the dedicated Colab SSH key; previous key kept at $backup"
fi
if [[ ! -f $KEY_PATH ]]; then
  mkdir -p "$(dirname -- "$KEY_PATH")"
  ssh-keygen -q -t ed25519 -N '' -C 'agent-sandbox-colab' -f "$KEY_PATH"
  log "Created a dedicated Colab SSH key: $KEY_PATH"
else
  log "Keeping existing Colab SSH key: $KEY_PATH"
fi
[[ -f "$KEY_PATH.pub" ]] || ssh-keygen -y -f "$KEY_PATH" > "$KEY_PATH.pub"
chmod 0600 "$KEY_PATH"
chmod 0644 "$KEY_PATH.pub"
ssh-keygen -y -f "$KEY_PATH" >/dev/null 2>&1 || die 'The dedicated Colab SSH key is invalid.'

if [[ $REFRESH_GIT -eq 1 || ! -f $COLAB_GITCONFIG ]]; then
  git_name=$(git config --global user.name 2>/dev/null || true)
  git_email=$(git config --global user.email 2>/dev/null || true)
  if [[ -n $git_name && -n $git_email && $git_name != *$'\n'* && $git_email != *$'\n'* ]]; then
    tmp=$(mktemp "${COLAB_GITCONFIG}.tmp.XXXXXX")
    {
      printf '%s\n' '[user]'
      printf '\tname = %s\n' "$git_name"
      printf '\temail = %s\n' "$git_email"
      printf '%s\n' '[init]'
      printf '\tdefaultBranch = main\n'
    } > "$tmp"
    chmod 0600 "$tmp"
    mv -f -- "$tmp" "$COLAB_GITCONFIG"
    log "Stored Git identity for the runtime ($git_name <$git_email>)."
  else
    warn 'No complete Git identity found; set user.name and user.email locally or run with --refresh-git-config later.'
  fi
fi

if [[ ! -f $COLAB_CONFIG ]]; then
  cat > "$COLAB_CONFIG" <<'CONF'
# Local Google Colab backend configuration. Never commit this file.
# Values may be overridden by exporting the same variable before running a script.
CONF
  chmod 0600 "$COLAB_CONFIG"
fi
config_set COLAB_SESSION "$COLAB_SESSION"
config_set COLAB_AUTH "$COLAB_AUTH"
config_set COLAB_STATE_FILE "$COLAB_STATE_FILE"
config_set COLAB_CLI "$(cd -- "$(dirname -- "$CLI_PATH")" && pwd)/$(basename -- "$CLI_PATH")"
if [[ $KEY_PATH == "$COLAB_REPO_ROOT/"* ]]; then
  config_set COLAB_SSH_KEY "${KEY_PATH#"$COLAB_REPO_ROOT/"}"
else
  config_set COLAB_SSH_KEY "$KEY_PATH"
fi
config_set COLAB_WORKSPACE "$COLAB_WORKSPACE"
config_set COLAB_ENDPOINT "$COLAB_ENDPOINT"
config_set COLAB_ACCELERATOR "$COLAB_ACCELERATOR"
config_set COLAB_TPU "$COLAB_TPU"
config_set COLAB_HIGH_MEM "$COLAB_HIGH_MEM"
config_set COLAB_CODEX_VERSION "$COLAB_CODEX_VERSION"
config_set COLAB_CLAUDE_VERSION "$COLAB_CLAUDE_VERSION"
config_set COLAB_OPENCODE_VERSION "$COLAB_OPENCODE_VERSION"
config_set COLAB_AGY_INSTALL "$COLAB_AGY_INSTALL"
config_set COLAB_SSH_WAIT_ATTEMPTS "$COLAB_SSH_WAIT_ATTEMPTS"
config_set COLAB_SSH_WAIT_SECONDS "$COLAB_SSH_WAIT_SECONDS"

# Reload the normalized configuration and generate the SSH alias immediately.
load_config
write_ssh_config

if [[ $SKIP_AUTH_CHECK != 1 ]]; then
  if [[ -t 0 ]]; then
    log 'Checking Google authentication (complete the sign-in flow if prompted)...'
    if ! check_auth_and_show_usage; then
      warn 'Authentication was not completed. Run `colab usage` in a terminal before `./scripts/colab/up.sh`.'
    fi
  else
    warn 'Skipping the interactive authentication check (no TTY). Run `colab usage` once in a terminal.'
  fi
fi

release_lock
trap - EXIT

cat <<TEXT

Colab backend is configured.
  Session:  $COLAB_SESSION
  SSH key:  $COLAB_SSH_KEY
  Workspace: $COLAB_WORKSPACE

Next:
  ./scripts/colab/up.sh          # provision CPU runtime and bootstrap it
  ./scripts/colab/connect.sh     # open an interactive SSH session
  ./scripts/colab/status.sh      # show runtime and compute-unit status
  ./scripts/colab/save.sh        # archive the remote workspace locally
  ./scripts/colab/down.sh        # release the runtime

Optional accelerators: ./scripts/colab/up.sh --gpu T4 (or --tpu v5e1)
TEXT
