#!/usr/bin/env bash
set -euo pipefail
umask 077

CODEX_REAL_BIN=${CODEX_REAL_BIN:-/usr/local/bin/codex-cli}
CODEX_HOME=${CODEX_HOME:-${HOME:?}/.codex}
CODEX_AUTH_DIR=${CODEX_AUTH_DIR:-${HOME:?}/.codex-auth}
AUTH_LINK="$CODEX_HOME/auth.json"
AUTH_TARGET="$CODEX_AUTH_DIR/auth.json"
AUTH_LOCK="$CODEX_AUTH_DIR/auth.lock"
AUTH_OPERATION="$CODEX_AUTH_DIR/auth-operation"

[[ -x "$CODEX_REAL_BIN" ]] || { echo 'Codex executable is missing.' >&2; exit 127; }
[[ -d "$CODEX_HOME" && -d "$CODEX_AUTH_DIR" ]] || { echo 'Codex authentication storage is not initialized.' >&2; exit 1; }
[[ -f "$AUTH_TARGET" && ! -L "$AUTH_TARGET" && -f "$AUTH_LOCK" && ! -L "$AUTH_LOCK" ]] \
  || { echo 'Codex authentication storage is not initialized.' >&2; exit 1; }

normalize_auth_link() {
  python3 - "$CODEX_HOME" "$CODEX_AUTH_DIR" <<'PYCODEXAUTH'
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile

home = Path(sys.argv[1])
store = Path(sys.argv[2])
link = home / "auth.json"
target = store / "auth.json"
lock_path = store / "auth.lock"

if target.stat().st_nlink != 1:
    raise SystemExit("persistent auth.json must not have hard links")

def atomic_store(data):
    json.loads(data)
    fd, temporary = tempfile.mkstemp(prefix=".auth-", dir=store)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

def is_unauthenticated(data):
    # Codex reports a bare {} auth.json as a ChatGPT login without a plan type.
    # The unauthenticated state must therefore be an absent auth.json.
    return json.loads(data) == {}

def remove_link():
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass

def install_link():
    temporary_link = home / f".auth-link-{os.getpid()}"
    try:
        os.symlink(str(target), temporary_link)
        os.replace(temporary_link, link)
    finally:
        try:
            temporary_link.unlink()
        except FileNotFoundError:
            pass

try:
    with open(lock_path, "r+b", buffering=0) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if os.path.islink(link):
            if os.readlink(link) != str(target):
                raise RuntimeError("unexpected CODEX_HOME auth.json symlink")
            if not target.is_file():
                raise RuntimeError("persistent auth.json is missing")
            if is_unauthenticated(target.read_bytes()):
                remove_link()
        elif os.path.lexists(link):
            if not link.is_file():
                raise RuntimeError("unexpected CODEX_HOME auth.json entry")
            data = link.read_bytes()
            atomic_store(data)
            if is_unauthenticated(data):
                remove_link()
            else:
                install_link()
        else:
            # Codex removes the symlink itself for logout and before login. Clearing
            # the target here makes that lifecycle operation durable across restarts.
            # No link is installed: an absent auth.json means "not logged in".
            atomic_store(b"{}\n")
except Exception as error:
    print(f"Codex credential link repair failed: {error}", file=sys.stderr)
    raise SystemExit(1)
PYCODEXAUTH
}

auth_operation=
args=("$@")
i=0
while (( i < ${#args[@]} )); do
  arg=${args[i]}
  case "$arg" in
    -c|--config|-p|--profile|--cd|--ask-for-approval|--sandbox|--model)
      i=$((i + 2))
      ;;
    login)
      if [[ "${args[i+1]:-}" != status ]]; then auth_operation=login; fi
      break
      ;;
    logout)
      auth_operation=logout
      break
      ;;
    --)
      break
      ;;
    -* )
      i=$((i + 1))
      ;;
    *)
      break
      ;;
  esac
done

normalize_auth_link
if [[ -n "$auth_operation" ]]; then
  if [[ -e "$AUTH_OPERATION" || -L "$AUTH_OPERATION" ]]; then
    echo 'A previous Codex authentication operation is incomplete; restart the sandbox before retrying.' >&2
    exit 1
  fi
  (set -C; printf '%s\n' "$auth_operation" > "$AUTH_OPERATION") || {
    echo 'Could not reserve Codex authentication state.' >&2
    exit 1
  }
fi

set +e
"$CODEX_REAL_BIN" "$@"
codex_status=$?
set -e

if ! normalize_auth_link; then
  echo 'Codex exited, but credential state could not be synchronized.' >&2
  exit 1
fi
if [[ -n "$auth_operation" ]]; then
  rm -f -- "$AUTH_OPERATION"
fi
exit "$codex_status"
