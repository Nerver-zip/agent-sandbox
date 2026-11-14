#!/usr/bin/env bash
set -euo pipefail
# Do not regenerate the host's identity, and never overwrite mounted tokens.
test -s /etc/ssh/ssh_host_ed25519_key
test -s /etc/ssh/ssh_host_ed25519_key.pub
CODEX_AUTH_DIR=/home/agent/.codex-auth
CODEX_AUTH_FILE="$CODEX_AUTH_DIR/auth.json"
CODEX_AUTH_LOCK="$CODEX_AUTH_DIR/auth.lock"
CODEX_AUTH_OPERATION="$CODEX_AUTH_DIR/auth-operation"
CODEX_HOME_AUTH=/home/agent/.codex/auth.json
CODEX_HOME_LOCK=/home/agent/.codex/auth.lock
test -d "$CODEX_AUTH_DIR" && test ! -L "$CODEX_AUTH_DIR"
test "$(stat -c '%u:%a' "$CODEX_AUTH_DIR")" = 10001:700
test -f "$CODEX_AUTH_FILE" && test ! -L "$CODEX_AUTH_FILE"
test "$(stat -c '%u:%a' "$CODEX_AUTH_FILE")" = 10001:600
test "$(stat -c '%h' "$CODEX_AUTH_FILE")" = 1
test -f "$CODEX_AUTH_LOCK" && test ! -L "$CODEX_AUTH_LOCK"
test "$(stat -c '%u:%a' "$CODEX_AUTH_LOCK")" = 10001:600
if [[ -e "$CODEX_AUTH_OPERATION" || -L "$CODEX_AUTH_OPERATION" ]]; then
  test -f "$CODEX_AUTH_OPERATION" && test ! -L "$CODEX_AUTH_OPERATION"
  test "$(stat -c '%u:%a' "$CODEX_AUTH_OPERATION")" = 10001:600
  AUTH_OPERATION=$(cat -- "$CODEX_AUTH_OPERATION")
  case "$AUTH_OPERATION" in
    login)
      if [[ -f "$CODEX_HOME_AUTH" && ! -L "$CODEX_HOME_AUTH" ]]; then
        # Login completed its atomic write before the wrapper was interrupted.
        RECOVERED_AUTH=$(mktemp "$CODEX_AUTH_DIR/.auth-recovery.XXXXXXXX")
        cat -- "$CODEX_HOME_AUTH" > "$RECOVERED_AUTH"
        python3 -m json.tool "$RECOVERED_AUTH" >/dev/null
        chown 10001:10001 "$RECOVERED_AUTH"
        chmod 0600 "$RECOVERED_AUTH"
        mv -f -- "$RECOVERED_AUTH" "$CODEX_AUTH_FILE"
        rm -f -- "$CODEX_HOME_AUTH"
      elif [[ -L "$CODEX_HOME_AUTH" ]]; then
        [[ "$(readlink -- "$CODEX_HOME_AUTH")" == "$CODEX_AUTH_FILE" ]] || {
          echo 'Unexpected Codex auth link during interrupted login.' >&2
          exit 1
        }
      else
        # Login removed the old symlink but did not finish saving a replacement.
        printf '{}\n' > "$CODEX_AUTH_FILE"
      fi
      ;;
    logout)
      printf '{}\n' > "$CODEX_AUTH_FILE"
      if [[ -f "$CODEX_HOME_AUTH" && ! -L "$CODEX_HOME_AUTH" ]]; then
        rm -f -- "$CODEX_HOME_AUTH"
      elif [[ -L "$CODEX_HOME_AUTH" ]]; then
        [[ "$(readlink -- "$CODEX_HOME_AUTH")" == "$CODEX_AUTH_FILE" ]] || {
          echo 'Unexpected Codex auth link during interrupted logout.' >&2
          exit 1
        }
      fi
      ;;
    import)
      if [[ -f "$CODEX_HOME_AUTH" && ! -L "$CODEX_HOME_AUTH" ]]; then
        rm -f -- "$CODEX_HOME_AUTH"
      elif [[ -L "$CODEX_HOME_AUTH" ]]; then
        [[ "$(readlink -- "$CODEX_HOME_AUTH")" == "$CODEX_AUTH_FILE" ]] || {
          echo 'Unexpected Codex auth link during interrupted import.' >&2
          exit 1
        }
      elif [[ -e "$CODEX_HOME_AUTH" ]]; then
        echo 'Unexpected Codex auth state during interrupted import.' >&2
        exit 1
      fi
      ;;
    *)
      echo 'Invalid Codex auth operation marker.' >&2
      exit 1
      ;;
  esac
  rm -f -- "$CODEX_AUTH_OPERATION"
fi
find "$CODEX_AUTH_DIR" -mindepth 1 -maxdepth 1 -type f -name '.auth-*' -delete
chown -R --no-dereference agent:agent /home/agent/.codex 2>/dev/null || true

ensure_codex_link() {
  local link=$1 target=$2
  if [[ -L "$link" ]]; then
    [[ "$(readlink -- "$link")" == "$target" ]] || {
      echo "Unexpected Codex credential link: $link" >&2
      exit 1
    }
  elif [[ -e "$link" ]]; then
    echo "Refusing to replace unexpected Codex state: $link" >&2
    exit 1
  else
    ln -s -- "$target" "$link"
  fi
  chown -h agent:agent "$link"
}

ensure_codex_link "$CODEX_HOME_AUTH" "$CODEX_AUTH_FILE"
ensure_codex_link "$CODEX_HOME_LOCK" "$CODEX_AUTH_LOCK"
test -f "$CODEX_HOME_AUTH" && test -f "$CODEX_HOME_LOCK"
test -f /home/agent/.claude/.credentials.json
chown -R agent:agent /home/agent/.claude 2>/dev/null || true
chmod 0755 /home/agent/.claude/statusline.sh 2>/dev/null || true
test -f /home/agent/.config/git/config
chown -R agent:agent /home/agent/.config/git 2>/dev/null || true
ln -sf /home/agent/.config/git/config /home/agent/.gitconfig 2>/dev/null || true
mkdir -p /run/sshd
chmod 0755 /run/sshd
rm -f /var/run/docker.sock
socat UNIX-LISTEN:/var/run/docker.sock,fork,mode=666,reuseaddr TCP:dind:2375 &
/usr/sbin/sshd -t -f /etc/ssh/sshd_config
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
