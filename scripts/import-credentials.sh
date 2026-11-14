#!/usr/bin/env bash
# Interactive helper: import Codex / Claude Code logins and a GitHub PAT into the
# persistent credential store. Only asks about services that are not configured yet.
# Called by setup.sh; can also be run on its own at any time while the sandbox is stopped.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077

if [[ ! -t 0 || ! -t 1 ]]; then
  echo 'An interactive terminal is required. Use scripts/import-codex-auth.sh, import-claude-auth.sh and set-gh-token.sh directly.' >&2
  exit 1
fi
if docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | grep -q .; then
  echo 'Sandbox is running; skipping credential import. Run docker compose stop, then ./scripts/import-credentials.sh.'
  exit 0
fi

priv() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
# {} (or a missing file) is the unauthenticated placeholder written by setup.sh.
unconfigured_json() { ! priv test -f "$1" || priv grep -qx '{}' "$1"; }

TMP_FILES=()
cleanup() { [[ ${#TMP_FILES[@]} -eq 0 ]] || rm -f -- "${TMP_FILES[@]}"; }
trap cleanup EXIT

# Ask where a login file lives. Accepts a local path or an scp-style source
# (user@host:path) so it can be pulled from the machine where you are already logged in.
# Result: SOURCE_FILE (private temporary copy) or empty when skipped.
ask_source() {
  local label=$1 default=$2 answer tmp
  SOURCE_FILE=
  if [[ -f "$default" ]]; then
    read -r -p "$label: found $default on this host. Import it? [Y/n] " answer
    if [[ -z "$answer" || "$answer" =~ ^[Yy] ]]; then answer=$default; else answer=; fi
  else
    answer=
  fi
  if [[ -z "$answer" ]]; then
    read -r -p "$label: path or user@host:path to import (Enter to skip): " answer
  fi
  [[ -n "$answer" ]] || return 0
  answer=${answer/#\~/$HOME}
  tmp=$(mktemp)
  TMP_FILES+=("$tmp")
  if [[ -f "$answer" ]]; then
    cp -L -- "$answer" "$tmp"
  elif [[ "$answer" == *:* ]]; then
    scp -q -- "$answer" "$tmp" || { echo "Could not copy $answer; skipping $label." >&2; return 0; }
  else
    echo "File not found: $answer; skipping $label." >&2
    return 0
  fi
  python3 -m json.tool "$tmp" >/dev/null 2>&1 || { echo "$label source is not valid JSON; skipping." >&2; return 0; }
  if [[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])) == {})' "$tmp")" == True ]]; then
    echo "$label source is an empty login ({}); skipping." >&2
    return 0
  fi
  SOURCE_FILE=$tmp
}

echo 'Credential import (press Enter to skip any item; you can always log in inside the sandbox instead).'

if unconfigured_json persistent/codex/auth.json; then
  ask_source 'Codex auth.json' "$HOME/.codex/auth.json"
  if [[ -n "$SOURCE_FILE" ]]; then ./scripts/import-codex-auth.sh "$SOURCE_FILE"; fi
else
  echo 'Codex: already configured.'
fi

if unconfigured_json persistent/claude/.credentials.json; then
  ask_source 'Claude .credentials.json' "$HOME/.claude/.credentials.json"
  if [[ -n "$SOURCE_FILE" ]]; then ./scripts/import-claude-auth.sh "$SOURCE_FILE"; fi
else
  echo 'Claude Code: already configured.'
fi

if ! priv test -s persistent/gh/token; then
  read -r -p 'GitHub: set a fine-grained PAT now? [y/N] ' answer
  if [[ "$answer" =~ ^[Yy] ]]; then ./scripts/set-gh-token.sh; fi
else
  echo 'GitHub token: already configured.'
fi
