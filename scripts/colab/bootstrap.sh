#!/usr/bin/env bash
# Bootstrap Agent Sandbox tooling inside a disposable Google Colab Ubuntu VM.
# This script runs as root on the remote runtime. It is idempotent.
set -Eeuo pipefail
umask 022

VERSION=
FORCE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      [[ $# -ge 2 ]] || { printf 'ERROR: --version requires a value.\n' >&2; exit 2; }
      VERSION=$2
      shift
      ;;
    --force) FORCE=1 ;;
    -h|--help)
      printf 'Usage: bootstrap.sh --version SHA256 [--force]\n'
      exit 0
      ;;
    *) printf 'ERROR: unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
  shift
done

[[ $VERSION =~ ^[0-9a-f]{64}$ ]] || { printf 'ERROR: invalid bootstrap version.\n' >&2; exit 2; }
[[ $EUID -eq 0 ]] || { printf 'ERROR: the Colab bootstrap must run as root.\n' >&2; exit 2; }

STATE_DIR=/root/.agent-sandbox
MARKER="$STATE_DIR/bootstrap-version"
LOG="$STATE_DIR/bootstrap.log"
mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"
exec > >(tee -a "$LOG") 2>&1
trap 'printf "[bootstrap] failed at line %s\n" "$LINENO" >&2' ERR

printf '\n[bootstrap] %s starting (version %s)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$VERSION"
if [[ -f $MARKER && "$(cat "$MARKER" 2>/dev/null || true)" == "$VERSION" && $FORCE -eq 0 ]]; then
  printf '[bootstrap] already current; nothing to do.\n'
  exit 0
fi

PAYLOAD_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -d $PAYLOAD_DIR/config ]] || { printf 'ERROR: bootstrap payload is incomplete (config/ missing).\n' >&2; exit 1; }
if [[ -f $PAYLOAD_DIR/env ]]; then
  # shellcheck disable=SC1091
  source "$PAYLOAD_DIR/env"
fi
: "${CODEX_VERSION:=latest}"
: "${CLAUDE_VERSION:=latest}"
: "${OPENCODE_VERSION:=latest}"
: "${AGY_INSTALL:=1}"

apt_get() {
  DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
    apt-get -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold "$@"
}

fetch() {
  curl -fsSL --retry 3 --retry-delay 2 "$1" -o "$2"
}

printf '[bootstrap] refreshing apt metadata\n'
apt_updated=0
for attempt in 1 2 3; do
  if apt_get update; then
    apt_updated=1
    break
  fi
  if [[ $attempt -lt 3 ]]; then
    printf '[bootstrap] apt update failed; retrying (%s/3)\n' "$attempt" >&2
    sleep 5
  fi
done
[[ $apt_updated -eq 1 ]] || { printf 'ERROR: apt-get update failed.\n' >&2; exit 1; }

CORE_PACKAGES=(
  ca-certificates curl wget git zsh tmux jq ripgrep fd-find fzf htop tree file less
  unzip zip tar xz-utils gzip bash-completion openssh-client python3-venv python3-pip
  pipx build-essential pkg-config make cmake dnsutils netcat-openbsd procps sudo
  zsh-autosuggestions zsh-syntax-highlighting
)
printf '[bootstrap] installing base packages\n'
if ! apt_get install -y --no-install-recommends "${CORE_PACKAGES[@]}"; then
  printf '[bootstrap] batch install failed; retrying packages individually\n' >&2
  for package in "${CORE_PACKAGES[@]}"; do
    apt_get install -y --no-install-recommends "$package" || printf '[bootstrap] warning: package %s unavailable\n' "$package" >&2
  done
fi

for package in gh eza neovim kitty-terminfo; do
  if ! dpkg -s "$package" >/dev/null 2>&1; then
    apt_get install -y --no-install-recommends "$package" || printf '[bootstrap] warning: package %s unavailable from apt\n' "$package" >&2
  fi
done

if ! command -v fd >/dev/null 2>&1 && command -v fdfind >/dev/null 2>&1; then
  ln -sfn "$(command -v fdfind)" /usr/local/bin/fd
fi

# GitHub CLI fallback for images whose apt repositories do not ship gh.
if ! command -v gh >/dev/null 2>&1; then
  GH_DEB_URL=$(curl -fsSL --retry 3 https://api.github.com/repos/cli/cli/releases/latest 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(a["browser_download_url"] for a in d["assets"] if a["name"].endswith("linux_amd64.deb")))' 2>/dev/null || true)
  if [[ -n $GH_DEB_URL ]]; then
    fetch "$GH_DEB_URL" /tmp/gh.deb && apt_get install -y /tmp/gh.deb || true
    rm -f /tmp/gh.deb
  fi
fi

# Starship is not packaged in Ubuntu 24.04.
if ! command -v starship >/dev/null 2>&1; then
  printf '[bootstrap] installing starship\n'
  tmp=$(mktemp -d)
  if fetch https://github.com/starship/starship/releases/latest/download/starship-x86_64-unknown-linux-musl.tar.gz "$tmp/starship.tar.gz"; then
    tar -xzf "$tmp/starship.tar.gz" -C "$tmp"
    install -m 0755 "$tmp/starship" /usr/local/bin/starship
  else
    printf '[bootstrap] warning: starship download failed\n' >&2
  fi
  rm -rf "$tmp"
fi

# eza fallback when the distribution package is unavailable.
if ! command -v eza >/dev/null 2>&1; then
  printf '[bootstrap] installing eza\n'
  tmp=$(mktemp -d)
  if fetch https://github.com/eza-community/eza/releases/latest/download/eza_x86_64-unknown-linux-musl.zip "$tmp/eza.zip"; then
    unzip -q -o "$tmp/eza.zip" -d "$tmp"
    install -m 0755 "$tmp/eza" /usr/local/bin/eza
  else
    printf '[bootstrap] warning: eza download failed\n' >&2
  fi
  rm -rf "$tmp"
fi

# fastfetch is not packaged in Ubuntu 24.04.
if ! command -v fastfetch >/dev/null 2>&1; then
  printf '[bootstrap] installing fastfetch\n'
  if fetch https://github.com/fastfetch-cli/fastfetch/releases/latest/download/fastfetch-linux-amd64.deb /tmp/fastfetch.deb; then
    apt_get install -y /tmp/fastfetch.deb || printf '[bootstrap] warning: fastfetch install failed\n' >&2
  else
    printf '[bootstrap] warning: fastfetch download failed\n' >&2
  fi
  rm -f /tmp/fastfetch.deb
fi

# Ubuntu's neovim is too old for the shared configuration; use the latest stable.
needs_nvim=0
if ! command -v nvim >/dev/null 2>&1; then
  needs_nvim=1
else
  nvim_line=$(nvim --version 2>/dev/null | sed -n '1p')
  nvim_minor=$(printf '%s\n' "$nvim_line" | sed -n 's/.*v0\.\([0-9][0-9]*\).*/\1/p')
  if [[ -z $nvim_minor || $nvim_minor -lt 10 ]]; then
    needs_nvim=1
  fi
fi
if [[ $needs_nvim -eq 1 ]]; then
  printf '[bootstrap] installing current neovim\n'
  tmp=$(mktemp -d)
  if fetch https://github.com/neovim/neovim/releases/latest/download/nvim-linux-x86_64.tar.gz "$tmp/nvim.tar.gz"; then
    mkdir -p "$tmp/nvim"
    tar -xzf "$tmp/nvim.tar.gz" -C "$tmp/nvim" --strip-components=1
    rm -rf /opt/nvim
    mv "$tmp/nvim" /opt/nvim
    ln -sfn /opt/nvim/bin/nvim /usr/local/bin/nvim
  else
    printf '[bootstrap] warning: neovim download failed; keeping the apt version\n' >&2
  fi
  rm -rf "$tmp"
fi

node_is_current() {
  command -v node >/dev/null 2>&1 || return 1
  local major
  major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || printf 0)
  [[ $major =~ ^[0-9]+$ ]] || return 1
  [[ $major -ge 20 ]]
}

if ! node_is_current; then
  printf '[bootstrap] installing Node.js 22 LTS\n'
  node_base=https://nodejs.org/dist/latest-v22.x
  node_sums=$(curl -fsSL --retry 3 "$node_base/SHASUMS256.txt") || { printf 'ERROR: could not fetch Node checksums.\n' >&2; exit 1; }
  node_tarball=$(printf '%s\n' "$node_sums" | awk '/linux-x64\.tar\.xz$/ {print $2; exit}')
  [[ -n $node_tarball ]] || { printf 'ERROR: could not resolve the Node.js tarball.\n' >&2; exit 1; }
  node_hash=$(printf '%s\n' "$node_sums" | awk -v f="$node_tarball" '$2==f {print $1; exit}')
  fetch "$node_base/$node_tarball" "/tmp/$node_tarball" || { printf 'ERROR: Node.js download failed.\n' >&2; exit 1; }
  (cd /tmp && printf '%s  %s\n' "$node_hash" "$node_tarball" | sha256sum -c -) \
    || { printf 'ERROR: Node.js checksum verification failed.\n' >&2; exit 1; }
  node_root=${node_tarball%.tar.xz}
  mkdir -p /usr/local/lib/nodejs
  rm -rf "/usr/local/lib/nodejs/$node_root"
  tar -xJf "/tmp/$node_tarball" -C /usr/local/lib/nodejs
  for binary in node npm npx corepack; do
    if [[ -e "/usr/local/lib/nodejs/$node_root/bin/$binary" ]]; then
      ln -sfn "/usr/local/lib/nodejs/$node_root/bin/$binary" "/usr/local/bin/$binary"
    fi
  done
  hash -r
  rm -f "/tmp/$node_tarball"
fi
node_is_current || { printf 'ERROR: Node.js 20+ is required but installation failed.\n' >&2; exit 1; }

# Node was extracted outside /usr/local/lib/node_modules, so npm's default
# global prefix points inside the versioned Node directory. Put global CLI
# binaries on PATH and keep them available to later interactive shells.
if [[ "$(npm config get prefix 2>/dev/null || true)" != /usr/local ]]; then
  npm config set prefix /usr/local
fi
chmod 0600 /root/.npmrc 2>/dev/null || true
mkdir -p /usr/local/lib/node_modules /usr/local/bin
export NPM_CONFIG_PREFIX=/usr/local

install_npm_tool() {
  local package=$1
  local binary=$2
  local version=$3
  if [[ $FORCE -eq 0 ]] && command -v "$binary" >/dev/null 2>&1 && "$binary" --version >/dev/null 2>&1; then
    printf '[bootstrap] %s already installed: %s\n' "$binary" "$("$binary" --version 2>/dev/null | head -n 1)"
    return 0
  fi
  printf '[bootstrap] installing %s@%s\n' "$package" "$version"
  npm install --global --no-audit --no-fund "$package@$version"
  command -v "$binary" >/dev/null 2>&1 || { printf 'ERROR: npm did not install %s.\n' "$binary" >&2; return 1; }
  "$binary" --version 2>/dev/null | head -n 1 || true
}

install_npm_tool '@openai/codex' codex "$CODEX_VERSION"
install_npm_tool '@anthropic-ai/claude-code' claude "$CLAUDE_VERSION"
install_npm_tool 'opencode-ai' opencode "$OPENCODE_VERSION"

if [[ $AGY_INSTALL == 1 ]] && ! command -v agy >/dev/null 2>&1; then
  printf '[bootstrap] installing Antigravity CLI\n'
  tmp=$(mktemp -d)
  if fetch https://antigravity.google/cli/install.sh "$tmp/install.sh"; then
    bash "$tmp/install.sh" || printf '[bootstrap] warning: Antigravity installer failed\n' >&2
  else
    printf '[bootstrap] warning: Antigravity installer download failed\n' >&2
  fi
  rm -rf "$tmp"
fi
if [[ -x /root/.local/bin/agy ]]; then
  ln -sfn /root/.local/bin/agy /usr/local/bin/agy
fi

if [[ ! -d /opt/oh-my-zsh/.git ]]; then
  printf '[bootstrap] installing Oh My Zsh\n'
  rm -rf /opt/oh-my-zsh
  git clone --depth 1 https://github.com/ohmyzsh/ohmyzsh.git /opt/oh-my-zsh \
    || { printf 'ERROR: Oh My Zsh clone failed.\n' >&2; exit 1; }
fi
chmod -R a+rX /opt/oh-my-zsh

printf '[bootstrap] installing shared Agent Sandbox configuration\n'
install -m 0644 "$PAYLOAD_DIR/config/agent.zshrc" /root/.zshrc
install -d -m 0755 /root/.config/starship
install -m 0644 "$PAYLOAD_DIR/config/starship.toml" /root/.config/starship/starship.toml
install -d -m 0700 /root/.codex
install -m 0644 "$PAYLOAD_DIR/config/codex.toml" /root/.codex/config.toml
install -d -m 0700 /root/.claude
cp -R "$PAYLOAD_DIR/config/claude/." /root/.claude/
chmod 0755 /root/.claude/statusline.sh
install -d -m 0700 /root/.gemini
cp -R "$PAYLOAD_DIR/config/gemini/." /root/.gemini/
install -d -m 0755 /root/.config/opencode
cp -R "$PAYLOAD_DIR/config/opencode/." /root/.config/opencode/
install -m 0644 "$PAYLOAD_DIR/config/tmux.conf" /root/.tmux.conf
install -d -m 0755 /root/.config/nvim
cp -R "$PAYLOAD_DIR/config/nvim/." /root/.config/nvim/
install -m 0755 "$PAYLOAD_DIR/agent-notify.sh" /usr/local/bin/agent-notify
install -d -m 0755 /root/.local/bin
ln -sfn /usr/local/bin/agent-notify /root/.local/bin/agent-notify
if [[ -f $PAYLOAD_DIR/gitconfig ]]; then
  install -m 0600 "$PAYLOAD_DIR/gitconfig" /root/.gitconfig
fi

# Keep the familiar /home/agent/workspace path on a runtime whose login is root.
if [[ ! -e /home/agent && ! -L /home/agent ]]; then
  ln -s /root /home/agent
fi
WORKSPACE=/home/agent/workspace
mkdir -p "$WORKSPACE"
if [[ ! -e /content/workspace && ! -L /content/workspace ]]; then
  ln -s "$WORKSPACE" /content/workspace 2>/dev/null || true
fi
if [[ ! -L /home/agent ]]; then
  for directory in .gemini .config .claude .codex .local; do
    if [[ ! -e "/home/agent/$directory" && ! -L "/home/agent/$directory" ]]; then
      ln -s "/root/$directory" "/home/agent/$directory"
    fi
  done
fi

if command -v zsh >/dev/null 2>&1; then
  zsh_path=$(command -v zsh)
  current_shell=$(getent passwd root 2>/dev/null | cut -d: -f7)
  if [[ -n $current_shell && $current_shell != "$zsh_path" ]] && command -v usermod >/dev/null 2>&1; then
    usermod -s "$zsh_path" root || printf '[bootstrap] warning: could not set zsh as root shell\n' >&2
  fi
fi

if command -v nvim >/dev/null 2>&1; then
  printf '[bootstrap] restoring Neovim plugins (best effort)\n'
  NVIM_LOG="$STATE_DIR/nvim-bootstrap.log"
  nvim --headless -c 'Lazy! restore' -c 'qa' >>"$NVIM_LOG" 2>&1 || true
  nvim --headless -c 'TSInstallSync! bash python json yaml javascript typescript' -c 'qa' >>"$NVIM_LOG" 2>&1 || true
  printf '[bootstrap] Neovim plugin setup finished\n'
fi

tool_version() {
  case "$1" in
    tmux) tmux -V 2>/dev/null | head -n 1 ;;
    *) "$1" --version 2>/dev/null | head -n 1 ;;
  esac
}

MISSING=()
for binary in zsh git gh nvim tmux starship eza codex claude opencode node npm python3 cmake; do
  if command -v "$binary" >/dev/null 2>&1; then
    printf '  %-10s %s\n' "$binary" "$(tool_version "$binary" || true)"
  else
    MISSING+=("$binary")
  fi
done
for binary in fastfetch agy; do
  if command -v "$binary" >/dev/null 2>&1; then
    printf '  %-10s %s\n' "$binary" "$(tool_version "$binary" || true)"
  else
    printf '[bootstrap] warning: optional tool missing: %s\n' "$binary" >&2
  fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  printf 'ERROR: required tools are missing: %s\n' "${MISSING[*]}" >&2
  exit 1
fi

printf '[bootstrap] recording completion marker\n'
tmp=$(mktemp "$STATE_DIR/.version.XXXXXX")
printf '%s\n' "$VERSION" > "$tmp"
chmod 0600 "$tmp"
mv -f -- "$tmp" "$MARKER"
date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/bootstrap-completed"
chmod 0600 "$STATE_DIR/bootstrap-completed"
printf '[bootstrap] complete: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
