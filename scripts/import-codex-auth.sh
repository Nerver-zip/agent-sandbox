#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 ]]; then
  echo 'Usage: ./scripts/import-codex-auth.sh /path/to/codex/auth.json' >&2
  exit 2
fi
SOURCE=$1
[[ -f "$SOURCE" && ! -L "$SOURCE" && -r "$SOURCE" ]] || { echo 'Source must be a readable regular file, not a symlink.' >&2; exit 1; }
SOURCE=$(realpath -- "$SOURCE")
CODEX_DIR="$PWD/persistent/codex"
AUTH_FILE="$CODEX_DIR/auth.json"

if [[ $EUID -eq 0 ]]; then
  test -d "$CODEX_DIR" && test ! -L "$CODEX_DIR" && test -f "$AUTH_FILE" && test ! -L "$AUTH_FILE" \
    || { echo 'Run scripts/setup.sh first; the Codex credential directory is not ready.' >&2; exit 1; }
else
  command -v sudo >/dev/null || { echo 'sudo is required to inspect the protected Codex credential directory.' >&2; exit 1; }
  if ! sudo test -d "$CODEX_DIR" || sudo test -L "$CODEX_DIR" || ! sudo test -f "$AUTH_FILE" || sudo test -L "$AUTH_FILE"; then
    echo 'Run scripts/setup.sh first; the Codex credential directory is not ready.' >&2
    exit 1
  fi
fi

python3 -m json.tool "$SOURCE" >/dev/null || { echo 'Invalid JSON source.' >&2; exit 1; }
# Apply only while stopped, avoiding races with Codex token refresh.
if docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | grep -q .; then
  echo 'Stop the sandbox with docker compose stop before importing credentials.' >&2
  exit 1
fi

# Mark the stopped container so its entrypoint discards any stale local auth file.
AUTH_OPERATION="$CODEX_DIR/auth-operation"
if [[ $EUID -eq 0 || -w "$CODEX_DIR" ]]; then
  if [[ -L "$AUTH_OPERATION" || ( -e "$AUTH_OPERATION" && ! -f "$AUTH_OPERATION" ) ]]; then
    echo 'Codex auth operation marker has an unsafe type.' >&2
    exit 1
  fi
  printf 'import\n' > "$AUTH_OPERATION"
  if [[ $EUID -eq 0 ]]; then chown 10001:10001 "$AUTH_OPERATION"; fi
  chmod 0600 "$AUTH_OPERATION"
else
  sudo sh -c 'if [ -L "$1" ] || { [ -e "$1" ] && [ ! -f "$1" ]; }; then exit 1; fi; printf "import\n" > "$1" && chown 10001:10001 "$1" && chmod 0600 "$1"' sh "$AUTH_OPERATION"
fi

TMP_PRIV=0
if [[ -w "$CODEX_DIR" ]]; then
  TMP=$(mktemp "$CODEX_DIR/.auth-import.XXXXXXXX")
else
  command -v sudo >/dev/null || { echo 'sudo is required to write the protected Codex credential directory.' >&2; exit 1; }
  TMP=$(sudo mktemp "$CODEX_DIR/.auth-import.XXXXXXXX")
  TMP_PRIV=1
fi
cleanup() {
  if [[ -n "${TMP:-}" ]]; then
    if [[ "$TMP_PRIV" == 1 ]]; then sudo rm -f -- "$TMP"; else rm -f -- "$TMP"; fi
  fi
}
trap cleanup EXIT

if [[ "$TMP_PRIV" == 1 ]]; then
  sudo sh -c 'cat -- "$1" > "$2" && chmod 0600 "$2" && python3 -m json.tool "$2" >/dev/null' sh "$SOURCE" "$TMP"
else
  cat -- "$SOURCE" > "$TMP"
  chmod 0600 "$TMP"
  python3 -m json.tool "$TMP" >/dev/null
fi

if [[ $EUID -eq 0 ]]; then
  chown 10001:10001 "$TMP"
  chmod 0600 "$TMP"
  mv -f -- "$TMP" "$AUTH_FILE"
elif [[ "$TMP_PRIV" == 1 ]]; then
  sudo chown 10001:10001 "$TMP"
  sudo chmod 0600 "$TMP"
  sudo mv -f -- "$TMP" "$AUTH_FILE"
else
  chmod 0600 "$TMP"
  mv -f -- "$TMP" "$AUTH_FILE"
fi
TMP=
printf 'Imported Codex authentication. Secrets are readable by the agent user.\n'
