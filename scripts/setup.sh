#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
for cmd in docker tailscale ssh-keygen awk; do command -v "$cmd" >/dev/null || fail "Missing command: $cmd"; done
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required.'

if [[ -n "${1:-}" ]]; then
  [[ -r "$1" ]] || fail "Public key missing: $1 (pass its path as argument)"
fi
TS_IP=$(tailscale ip -4 2>/dev/null | head -n 1)
[[ "$TS_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'Tailscale host IPv4 not detected.'

install -d -m 0700 persistent persistent/ssh persistent/codex persistent/gh ssh
HOST_KEY="$PWD/persistent/ssh/ssh_host_ed25519_key"
HOST_PUB="$HOST_KEY.pub"
if [[ -f "$HOST_KEY" ]]; then
  [[ -f "$HOST_PUB" ]] || fail 'SSH host private key exists, but public key is missing.'
  echo 'Keeping existing SSH server identity.'
else
  [[ ! -e "$HOST_PUB" ]] || fail 'Orphaned SSH host public key. Fix manually.'
  # Migrate from the previous persistent layout when available, preserving known_hosts.
  if [[ -s ssh/host_keys/ssh_host_ed25519_key && -s ssh/host_keys/ssh_host_ed25519_key.pub ]]; then
    cp ssh/host_keys/ssh_host_ed25519_key "$HOST_KEY"
    cp ssh/host_keys/ssh_host_ed25519_key.pub "$HOST_PUB"
    echo 'Migrated SSH identity from previous ssh/host_keys layout.'
  else
  # Migrate the live container's exact host key, avoiding known_hosts warnings.
  CID=$(docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | head -n 1)
  if [[ -n "$CID" ]]; then
    TMP_PRIV=$(mktemp "${HOST_KEY}.tmp.XXXXXXXX")
    TMP_PUB=$(mktemp "${HOST_PUB}.tmp.XXXXXXXX")
    trap 'rm -f -- "$TMP_PRIV" "$TMP_PUB"' EXIT
    docker exec "$CID" cat /etc/ssh/ssh_host_ed25519_key > "$TMP_PRIV" || fail 'Could not extract SSH host private key.'
    docker exec "$CID" cat /etc/ssh/ssh_host_ed25519_key.pub > "$TMP_PUB" || fail 'Could not extract SSH host public key.'
    chmod 0600 "$TMP_PRIV"
    ssh-keygen -y -f "$TMP_PRIV" >/dev/null 2>&1 || fail 'Extracted SSH host key is invalid.'
    mv -f "$TMP_PRIV" "$HOST_KEY"
    mv -f "$TMP_PUB" "$HOST_PUB"
    trap - EXIT
    echo 'Migrated SSH server identity from currently running container.'
  else
    ssh-keygen -q -t ed25519 -N '' -f "$HOST_KEY"
    echo 'Created a new SSH server identity.'
  fi
  fi
fi
chmod 0600 "$HOST_KEY"
chmod 0644 "$HOST_PUB"
EXPECTED=$(awk '{print $1, $2}' "$HOST_PUB")
DERIVED=$(ssh-keygen -y -f "$HOST_KEY" | awk '{print $1, $2}') || fail 'SSH host private key invalid.'
[[ "$EXPECTED" == "$DERIVED" ]] || fail 'SSH server private/public keys do not match.'
ssh-keygen -lf "$HOST_PUB"

# Persist ONLY the Codex auth file from CODEX_HOME; SSH identity and GH PAT are separate allowlisted files.
# {} is valid unauthenticated state, unlike an empty (invalid JSON) file.
AUTH_FILE="$PWD/persistent/codex/auth.json"
if [[ ! -e "$AUTH_FILE" ]]; then
  printf '{}\n' > "$AUTH_FILE"
  # Migrate the CURRENT (running) Codex file, if present. No secret is printed.
  OLD_CID=$(docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | head -n 1)
  if [[ -n "$OLD_CID" ]]; then
    MIGRATED=$(mktemp)
    if docker exec "$OLD_CID" cat /home/agent/.codex/auth.json > "$MIGRATED" 2>/dev/null && \
       [[ -s "$MIGRATED" ]] && python3 -m json.tool "$MIGRATED" >/dev/null 2>&1; then
      cat "$MIGRATED" > "$AUTH_FILE"
      echo 'Migrated Codex login from running container.'
    fi
    rm -f "$MIGRATED"
  fi
fi
[[ -f "$AUTH_FILE" && ! -L "$AUTH_FILE" ]] || fail 'Codex auth must be a regular file, not a symlink.'

run_priv() {
  local cmd=$1
  if [[ $EUID -eq 0 ]]; then
    sh -c "$cmd"
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo sh -c "$cmd"
  elif command -v docker >/dev/null 2>&1 && docker image inspect agent-sandbox:local >/dev/null 2>&1; then
    docker run --rm -v "$PWD:/w" -w /w --entrypoint sh agent-sandbox:local -c "$cmd"
  else
    command -v sudo >/dev/null || fail 'sudo or docker is required for privileged setup operations.'
    sudo sh -c "$cmd"
  fi
}

if [[ -r "$AUTH_FILE" ]]; then
  python3 -m json.tool "$AUTH_FILE" >/dev/null || fail 'Codex auth file is invalid JSON.'
else
  run_priv "python3 -m json.tool \"$AUTH_FILE\" >/dev/null" || fail 'Codex auth file is invalid JSON.'
fi
# Running container user is UID 10001; keep this secret 0600 and inaccessible to others.
chmod 0600 "$AUTH_FILE" 2>/dev/null || true
if [[ "$(stat -c '%u' "$AUTH_FILE")" != 10001 ]]; then
  run_priv "chown 10001:10001 \"$AUTH_FILE\""
fi
[[ "$(stat -c '%a' "$AUTH_FILE")" == 600 ]] || fail 'Codex auth file must have mode 0600.'

# Optional fine-grained GitHub token. Empty = unauthenticated gh until set-gh-token.sh.
GH_TOKEN_FILE="$PWD/persistent/gh/token"
if [[ ! -e "$GH_TOKEN_FILE" ]]; then
  : > "$GH_TOKEN_FILE"
fi
[[ -f "$GH_TOKEN_FILE" && ! -L "$GH_TOKEN_FILE" ]] || fail 'GitHub token must be a regular file, not a symlink.'
if [[ $(stat -c '%u:%a' "$GH_TOKEN_FILE") != 10001:400 ]]; then
  run_priv "chown 10001:10001 \"$GH_TOKEN_FILE\" && chmod 0400 \"$GH_TOKEN_FILE\""
fi
[[ $(stat -c '%u:%a' "$GH_TOKEN_FILE") == 10001:400 ]] || fail 'GitHub token must be owned by UID 10001, mode 0400.'

# Persist authorized client public keys across container recreations.
# Supports single-key .pub files and multi-key authorized_keys files (e.g. PC, laptop, phones).
AUTH_KEYS_FILE="$PWD/persistent/ssh/authorized_keys"
TMP_RAW_KEYS=$(mktemp persistent/ssh/.raw.ak.XXXXXXXX)
TMP_DEDUP_KEYS=$(mktemp persistent/ssh/.dedup.ak.XXXXXXXX)
trap 'rm -f -- persistent/ssh/.raw.ak.* persistent/ssh/.dedup.ak.* persistent/ssh/.dump.* persistent/ssh/.install.*' EXIT

collect_keys_from_stream() {
  local label=$1
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    IFS=' ' read -r KEY_TYPE KEY_DATA KEY_COMMENT <<< "$line"
    [[ "$KEY_TYPE" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))$ && "$KEY_DATA" =~ ^[A-Za-z0-9+/=]+$ ]] \
      || fail "Expected PUBLIC SSH key in $label, never a private key."
    printf '%s\n' "$line" >> "$TMP_RAW_KEYS"
  done
}

if [[ -e "$AUTH_KEYS_FILE" ]]; then
  [[ -f "$AUTH_KEYS_FILE" && ! -L "$AUTH_KEYS_FILE" ]] || fail 'Persistent authorized_keys must be a regular file, not a symlink.'
  if [[ -r "$AUTH_KEYS_FILE" ]]; then
    collect_keys_from_stream "$AUTH_KEYS_FILE" < "$AUTH_KEYS_FILE"
  else
    TMP_DUMP=$(mktemp persistent/ssh/.dump.ak.XXXXXXXX)
    run_priv "cat \"$AUTH_KEYS_FILE\" > \"$TMP_DUMP\" && chmod 0600 \"$TMP_DUMP\""
    collect_keys_from_stream "$AUTH_KEYS_FILE" < "$TMP_DUMP"
    rm -f "$TMP_DUMP"
  fi
fi
if [[ -f ssh/authorized_keys && -r ssh/authorized_keys ]]; then
  collect_keys_from_stream "ssh/authorized_keys" < ssh/authorized_keys
fi
OLD_CID=$(docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | head -n 1)
if [[ -n "${OLD_CID:-}" ]]; then
  MIGRATED_AK=$(mktemp persistent/ssh/.migrated.ak.XXXXXXXX)
  if docker exec "$OLD_CID" cat /home/agent/.ssh/authorized_keys > "$MIGRATED_AK" 2>/dev/null && [[ -s "$MIGRATED_AK" ]]; then
    collect_keys_from_stream "running container" < "$MIGRATED_AK"
  fi
  rm -f "$MIGRATED_AK"
fi
if [[ -n "${1:-}" ]]; then
  collect_keys_from_stream "$1" < "$1"
else
  [[ -r "$HOME/.ssh/id_ed25519.pub" ]] && collect_keys_from_stream "$HOME/.ssh/id_ed25519.pub" < "$HOME/.ssh/id_ed25519.pub"
  [[ -r "$HOME/.ssh/authorized_keys" ]] && collect_keys_from_stream "$HOME/.ssh/authorized_keys" < "$HOME/.ssh/authorized_keys"
fi

awk '!seen[$1" "$2]++' "$TMP_RAW_KEYS" > "$TMP_DEDUP_KEYS"
[[ -s "$TMP_DEDUP_KEYS" ]] || fail 'No valid SSH public keys found. Pass a public key or authorized_keys file.'
ssh-keygen -lf "$TMP_DEDUP_KEYS" >/dev/null 2>&1 || fail 'Invalid SSH public key.'
install -m 0600 "$TMP_DEDUP_KEYS" ssh/authorized_keys
if [[ ! -e "$AUTH_KEYS_FILE" ]]; then
  : > "$AUTH_KEYS_FILE"
fi
TMP_INSTALL=$(mktemp persistent/ssh/.install.ak.XXXXXXXX)
cp "$TMP_DEDUP_KEYS" "$TMP_INSTALL"
run_priv "cat \"$TMP_INSTALL\" > \"$AUTH_KEYS_FILE\" && chown 10001:10001 \"$AUTH_KEYS_FILE\" && chmod 0600 \"$AUTH_KEYS_FILE\""
rm -f "$TMP_INSTALL"
[[ $(stat -c '%u:%a' "$AUTH_KEYS_FILE") == 10001:600 ]] || fail 'Persistent authorized_keys must be owned by UID 10001, mode 0600.'
rm -f -- persistent/ssh/.raw.ak.* persistent/ssh/.dedup.ak.*
trap - EXIT

# Persist outgoing SSH host key verifications (known_hosts) across container recreations.
KNOWN_HOSTS_FILE="$PWD/persistent/ssh/known_hosts"
if [[ ! -e "$KNOWN_HOSTS_FILE" ]]; then
  : > "$KNOWN_HOSTS_FILE"
  if [[ -n "${OLD_CID:-}" ]]; then
    MIGRATED_KH=$(mktemp persistent/ssh/.migrated.kh.XXXXXXXX)
    if docker exec "$OLD_CID" cat /home/agent/.ssh/known_hosts > "$MIGRATED_KH" 2>/dev/null && [[ -s "$MIGRATED_KH" ]]; then
      cat "$MIGRATED_KH" > "$KNOWN_HOSTS_FILE"
      echo 'Migrated SSH known_hosts from running container.'
    fi
    rm -f "$MIGRATED_KH"
  fi
fi
[[ -f "$KNOWN_HOSTS_FILE" && ! -L "$KNOWN_HOSTS_FILE" ]] || fail 'SSH known_hosts must be a regular file, not a symlink.'
if [[ $(stat -c '%u:%a' "$KNOWN_HOSTS_FILE") != 10001:600 ]]; then
  run_priv "chown 10001:10001 \"$KNOWN_HOSTS_FILE\" && chmod 0600 \"$KNOWN_HOSTS_FILE\""
fi
[[ $(stat -c '%u:%a' "$KNOWN_HOSTS_FILE") == 10001:600 ]] || fail 'SSH known_hosts file must be owned by UID 10001, mode 0600.'
if [[ -f .env ]]; then
  # Fix IP changes while retaining user-provided memory/CPU/port settings.
  [[ -L .env ]] && fail '.env must not be a symlink.'
  python3 - "$TS_IP" <<'PYENV'
from pathlib import Path
import sys
p=Path('.env')
lines=p.read_text().splitlines()
lines=[f'TAILSCALE_IP={sys.argv[1]}' if l.startswith('TAILSCALE_IP=') else l for l in lines]
if not any(l.startswith('TAILSCALE_IP=') for l in lines):lines.insert(0, f'TAILSCALE_IP={sys.argv[1]}')
p.write_text('\n'.join(lines)+'\n')
PYENV
  echo 'Kept .env settings and refreshed Tailscale IP.'
else
  cat > .env <<CONF
TAILSCALE_IP=$TS_IP
AGENT_SSH_PORT=2222
AGENT_MEMORY=3g
AGENT_CPUS=2.0
CODEX_VERSION=latest
DIND_MEMORY=4g
DIND_CPUS=2.0
CONF
fi
chmod 0600 .env
printf '\nSetup complete. Tailscale host IP: %s\n' "$TS_IP"
echo 'Run ./scripts/up.sh to deploy.'
echo 'SSH host identity, Codex auth and optional GitHub PAT persist across docker compose down.'
