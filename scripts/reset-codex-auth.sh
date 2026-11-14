#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
CODEX_DIR="$PWD/persistent/codex"
AUTH_FILE="$CODEX_DIR/auth.json"
AUTH_OPERATION="$CODEX_DIR/auth-operation"

if [[ $EUID -eq 0 ]]; then
  CODEX_DIR_STATE=$(stat -c '%F:%u:%a' "$CODEX_DIR" 2>/dev/null || true)
  AUTH_FILE_STATE=$(stat -c '%F:%u:%a:%h' "$AUTH_FILE" 2>/dev/null || true)
else
  command -v sudo >/dev/null || { echo 'sudo is required to inspect the protected Codex credential directory.' >&2; exit 1; }
  sudo -v || { echo 'sudo authorization is required to reset Codex authentication.' >&2; exit 1; }
  CODEX_DIR_STATE=$(sudo stat -c '%F:%u:%a' "$CODEX_DIR" 2>/dev/null || true)
  AUTH_FILE_STATE=$(sudo stat -c '%F:%u:%a:%h' "$AUTH_FILE" 2>/dev/null || true)
fi
[[ "$CODEX_DIR_STATE" == 'directory:10001:700' ]] || { echo 'Codex credential directory is missing or has unsafe permissions.' >&2; exit 1; }
[[ "$AUTH_FILE_STATE" == 'regular file:10001:600:1' ]] || { echo 'Codex auth file is missing or has unsafe permissions.' >&2; exit 1; }

if docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | grep -q .; then
  echo 'Stop the sandbox with docker compose stop before resetting credentials.' >&2
  exit 1
fi

# Mark the stopped container before changing the durable auth document. If the
# reset is interrupted, the entrypoint will discard any stale local auth file.
if [[ $EUID -eq 0 ]]; then
  if [[ -L "$AUTH_OPERATION" || ( -e "$AUTH_OPERATION" && ! -f "$AUTH_OPERATION" ) ]]; then
    echo 'Codex auth operation marker has an unsafe type.' >&2
    exit 1
  fi
  printf 'logout\n' > "$AUTH_OPERATION"
  chown 10001:10001 "$AUTH_OPERATION"
  chmod 0600 "$AUTH_OPERATION"
else
  sudo sh -c 'if [ -L "$1" ] || { [ -e "$1" ] && [ ! -f "$1" ]; }; then exit 1; fi; printf "logout\n" > "$1" && chown 10001:10001 "$1" && chmod 0600 "$1"' sh "$AUTH_OPERATION"
fi

RESET_TMP_PRIV=0
if [[ -w "$CODEX_DIR" ]]; then
  RESET_TMP=$(mktemp "$CODEX_DIR/.auth-reset.XXXXXXXX")
else
  RESET_TMP=$(sudo mktemp "$CODEX_DIR/.auth-reset.XXXXXXXX")
  RESET_TMP_PRIV=1
fi
cleanup() {
  if [[ -n "$RESET_TMP" ]]; then
    if [[ "$RESET_TMP_PRIV" == 1 ]]; then sudo rm -f -- "$RESET_TMP"; else rm -f -- "$RESET_TMP"; fi
  fi
}
trap cleanup EXIT

if [[ $EUID -eq 0 ]]; then
  printf '{}\n' > "$RESET_TMP"
  chown 10001:10001 "$RESET_TMP"
  chmod 0600 "$RESET_TMP"
  mv -f -- "$RESET_TMP" "$AUTH_FILE"
elif [[ "$RESET_TMP_PRIV" == 1 ]]; then
  sudo sh -c 'printf "{}\n" > "$1" && chown 10001:10001 "$1" && chmod 0600 "$1" && mv -f -- "$1" "$2"' sh "$RESET_TMP" "$AUTH_FILE"
else
  printf '{}\n' > "$RESET_TMP"
  chmod 0600 "$RESET_TMP"
  mv -f -- "$RESET_TMP" "$AUTH_FILE"
fi
RESET_TMP=
echo 'Persisted Codex authentication reset. Start the sandbox and log in again.'
