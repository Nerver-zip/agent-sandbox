#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo 'Usage: ./scripts/import-git-config.sh /path/to/.gitconfig' >&2
  echo '   or: ./scripts/import-git-config.sh "Author Name" "author@example.com"' >&2
  exit 2
fi

SOURCE=
if [[ $# -eq 1 ]]; then
  SOURCE=$(realpath -- "$1" 2>/dev/null || echo "$1")
fi

cd "$(dirname "$0")/.."
GIT_CONFIG_FILE="$PWD/persistent/git/config"
[[ -d persistent/git ]] || { echo 'Run scripts/setup.sh first.' >&2; exit 1; }

# Apply only while stopped to prevent writing while git might be in use
if docker ps -q --filter 'label=com.docker.compose.project=agent-sandbox' --filter 'label=com.docker.compose.service=workspace' | grep -q .; then
  echo 'Stop the sandbox with docker compose stop before importing Git configuration.' >&2
  exit 1
fi

GIT_NAME=
GIT_EMAIL=

if [[ $# -eq 1 ]]; then
  [[ -f "$SOURCE" && ! -L "$SOURCE" && -r "$SOURCE" ]] || { echo 'Source must be a readable regular file, not a symlink.' >&2; exit 1; }
  GIT_NAME=$(git config -f "$SOURCE" user.name 2>/dev/null || true)
  GIT_EMAIL=$(git config -f "$SOURCE" user.email 2>/dev/null || true)
  [[ -n "$GIT_NAME" ]] || { echo "Source file ($SOURCE) is missing user.name." >&2; exit 1; }
  [[ -n "$GIT_EMAIL" ]] || { echo "Source file ($SOURCE) is missing user.email." >&2; exit 1; }
else
  GIT_NAME=$1
  GIT_EMAIL=$2
  [[ -n "$GIT_NAME" ]] || { echo 'Author name cannot be empty.' >&2; exit 1; }
  [[ -n "$GIT_EMAIL" && "$GIT_EMAIL" == *@* ]] || { echo 'Author email must be a valid email address.' >&2; exit 1; }
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
cat > "$TMP" <<EOF
[user]
	name = $GIT_NAME
	email = $GIT_EMAIL
[init]
	defaultBranch = main
EOF

# DO NOT replace bind-mounted inode: write in-place.
if [[ -w "$GIT_CONFIG_FILE" ]]; then
  cat "$TMP" > "$GIT_CONFIG_FILE"
  chmod 0600 "$GIT_CONFIG_FILE" 2>/dev/null || true
elif [[ $EUID -eq 0 ]]; then
  cat "$TMP" > "$GIT_CONFIG_FILE"
  chown 10001:10001 "$GIT_CONFIG_FILE"
  chmod 0600 "$GIT_CONFIG_FILE"
else
  sudo sh -c 'cat "$1" > "$2" && chown 10001:10001 "$2" && chmod 0600 "$2"' sh "$TMP" "$GIT_CONFIG_FILE"
fi

printf 'Imported Git identity (%s <%s>).\n' "$GIT_NAME" "$GIT_EMAIL"
