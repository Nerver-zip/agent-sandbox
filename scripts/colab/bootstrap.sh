#!/usr/bin/env bash
# Bootstrap Agent Sandbox tooling inside a disposable Google Colab Ubuntu VM.
# This script runs as root on the remote runtime. It is idempotent.
#
# Independent installation tasks run concurrently so the critical path is the
# slowest package source rather than the sum of every download. Every task is
# still validated before the runtime is reported ready.
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
NVIM_CACHE_MARKER="$STATE_DIR/nvim-cache-key"
mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"
exec > >(tee -a "$LOG") 2>&1
trap 'printf "[bootstrap] failed at line %s\n" "$LINENO" >&2' ERR

BOOT_START=$(date +%s)
milestone() {
  printf '[bootstrap] milestone: %s at %ss\n' "$1" "$(( $(date +%s) - BOOT_START ))"
}

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

AGENT_PREFIX_BASE=/opt/agent-sandbox/agents
CORE_PACKAGES=(
  ca-certificates curl wget git zsh tmux jq ripgrep fd-find fzf htop tree file less
  unzip zip tar xz-utils gzip bash-completion openssh-client python3-venv python3-pip
  pipx build-essential pkg-config make cmake dnsutils netcat-openbsd procps sudo
  zsh-autosuggestions zsh-syntax-highlighting
)
OPTIONAL_APT_PACKAGES=(gh eza kitty-terminfo)
# Neovim is deliberately not an apt package: the shared configuration requires
# a current upstream build, and apt's 0.9.x plus its runtime would be dead weight.

apt_get() {
  DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
    apt-get -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold "$@"
}

fetch() {
  curl -fsSL --retry 3 --retry-delay 2 "$1" -o "$2"
}

wait_for_command() {
  local command_name=$1
  local attempts=${2:-30}
  local attempt=1
  while [[ $attempt -le $attempts ]]; do
    command -v "$command_name" >/dev/null 2>&1 && return 0
    sleep 2
    attempt=$((attempt + 1))
  done
  return 1
}

package_installed() {
  dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null | grep -q '^ii'
}

install_configuration() {
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
}

set_default_shell() {
  if command -v zsh >/dev/null 2>&1; then
    local zsh_path current_shell
    zsh_path=$(command -v zsh)
    current_shell=$(getent passwd root 2>/dev/null | cut -d: -f7)
    if [[ -n $current_shell && $current_shell != "$zsh_path" ]] && command -v usermod >/dev/null 2>&1; then
      usermod -s "$zsh_path" root || printf '[bootstrap] warning: could not set zsh as root shell\n' >&2
    fi
  fi
}

install_apt_task() {
  local missing=()
  local optional_missing=()
  local package
  for package in "${CORE_PACKAGES[@]}"; do
    package_installed "$package" || missing+=("$package")
  done
  for package in "${OPTIONAL_APT_PACKAGES[@]}"; do
    package_installed "$package" || optional_missing+=("$package")
  done
  if [[ ${#missing[@]} -eq 0 && ${#optional_missing[@]} -eq 0 ]]; then
    printf '[bootstrap] all apt packages are already installed; skipping apt\n'
    return 0
  fi
  printf '[bootstrap] refreshing apt metadata and installing %s required and %s optional package(s)\n' \
    "${#missing[@]}" "${#optional_missing[@]}"
  local apt_updated=0
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
  [[ $apt_updated -eq 1 ]] || { printf 'ERROR: apt-get update failed.\n' >&2; return 1; }
  if [[ ${#missing[@]} -gt 0 ]]; then
    if ! apt_get install -y --no-install-recommends "${missing[@]}"; then
      printf '[bootstrap] batch install failed; retrying packages individually\n' >&2
      for package in "${missing[@]}"; do
        apt_get install -y --no-install-recommends "$package" \
          || printf '[bootstrap] warning: package %s unavailable\n' "$package" >&2
      done
    fi
  fi
  for package in "${optional_missing[@]}"; do
    apt_get install -y --no-install-recommends "$package" \
      || printf '[bootstrap] warning: optional package %s unavailable from apt\n' "$package" >&2
  done
}

install_node_task() {
  node_is_current() {
    command -v node >/dev/null 2>&1 || return 1
    local major
    major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || printf 0)
    [[ $major =~ ^[0-9]+$ ]] || return 1
    [[ $major -ge 20 ]]
  }
  if node_is_current; then
    printf '[bootstrap] Node.js is already current: %s\n' "$(node --version)"
    return 0
  fi
  printf '[bootstrap] installing Node.js 22 LTS\n'
  local node_base=https://nodejs.org/dist/latest-v22.x
  local node_sums node_tarball node_hash node_root binary
  node_sums=$(curl -fsSL --retry 3 "$node_base/SHASUMS256.txt") \
    || { printf 'ERROR: could not fetch Node checksums.\n' >&2; return 1; }
  node_tarball=$(printf '%s\n' "$node_sums" | awk '/linux-x64\.tar\.xz$/ {print $2; exit}')
  [[ -n $node_tarball ]] || { printf 'ERROR: could not resolve the Node.js tarball.\n' >&2; return 1; }
  node_hash=$(printf '%s\n' "$node_sums" | awk -v f="$node_tarball" '$2==f {print $1; exit}')
  fetch "$node_base/$node_tarball" "/tmp/$node_tarball" \
    || { printf 'ERROR: Node.js download failed.\n' >&2; return 1; }
  (cd /tmp && printf '%s  %s\n' "$node_hash" "$node_tarball" | sha256sum -c -) \
    || { printf 'ERROR: Node.js checksum verification failed.\n' >&2; return 1; }
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
  node_is_current || { printf 'ERROR: Node.js 20+ is required but installation failed.\n' >&2; return 1; }
}

configure_npm() {
  if [[ "$(npm config get prefix 2>/dev/null || true)" != /usr/local ]]; then
    npm config set prefix /usr/local
  fi
  chmod 0600 /root/.npmrc 2>/dev/null || true
  mkdir -p /usr/local/lib/node_modules /usr/local/bin
  export NPM_CONFIG_PREFIX=/usr/local
}

install_npm_tool() {
  local package=$1
  local binary=$2
  local version=$3
  local name=$4
  local prefix="$AGENT_PREFIX_BASE/$name"
  local installed=
  if [[ $FORCE -eq 0 ]] && command -v "$binary" >/dev/null 2>&1 && "$binary" --version >/dev/null 2>&1; then
    printf '[bootstrap] %s already installed: %s\n' "$binary" "$("$binary" --version 2>/dev/null | head -n 1)"
    return 0
  fi
  printf '[bootstrap] installing %s@%s in %s\n' "$package" "$version" "$prefix"
  mkdir -p "$prefix"
  # Separate prefixes let the three global installers run concurrently without
  # racing on /usr/local/lib/node_modules. The binaries are linked into PATH.
  npm install --global --no-audit --no-fund --prefix "$prefix" "$package@$version" || return 1
  [[ -x "$prefix/bin/$binary" ]] || { printf 'ERROR: npm did not install %s.\n' "$binary" >&2; return 1; }
  ln -sfn "$prefix/bin/$binary" "/usr/local/bin/$binary"
  hash -r 2>/dev/null || true
  command -v "$binary" >/dev/null 2>&1 || { printf 'ERROR: %s is not on PATH.\n' "$binary" >&2; return 1; }
  installed=$("$binary" --version 2>/dev/null | head -n 1 || true)
  [[ -n $installed ]] || { printf 'ERROR: %s did not run after installation.\n' "$binary" >&2; return 1; }
  printf '[bootstrap] %s %s\n' "$binary" "$installed"
}

install_npm_tools_task() {
  configure_npm
  local pids=() names=() rc=0
  install_npm_tool '@openai/codex' codex "$CODEX_VERSION" codex & pids+=("$!"); names+=(codex)
  install_npm_tool '@anthropic-ai/claude-code' claude "$CLAUDE_VERSION" claude & pids+=("$!"); names+=(claude)
  install_npm_tool 'opencode-ai' opencode "$OPENCODE_VERSION" opencode & pids+=("$!"); names+=(opencode)
  local i
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || { printf '[bootstrap] warning: %s installation failed\n' "${names[$i]}" >&2; rc=1; }
  done
  return "$rc"
}

install_starship_task() {
  if command -v starship >/dev/null 2>&1 && starship --version >/dev/null 2>&1; then
    return 0
  fi
  printf '[bootstrap] installing starship\n'
  local tmp
  tmp=$(mktemp -d)
  if fetch https://github.com/starship/starship/releases/latest/download/starship-x86_64-unknown-linux-musl.tar.gz "$tmp/starship.tar.gz"; then
    tar -xzf "$tmp/starship.tar.gz" -C "$tmp"
    install -m 0755 "$tmp/starship" /usr/local/bin/starship
  else
    printf '[bootstrap] warning: starship download failed\n' >&2
    rm -rf "$tmp"
    return 0
  fi
  rm -rf "$tmp"
}

install_eza_task() {
  if command -v eza >/dev/null 2>&1; then
    return 0
  fi
  printf '[bootstrap] installing eza\n'
  local tmp
  tmp=$(mktemp -d)
  if fetch https://github.com/eza-community/eza/releases/latest/download/eza_x86_64-unknown-linux-musl.zip "$tmp/eza.zip"; then
    unzip -q -o "$tmp/eza.zip" -d "$tmp"
    install -m 0755 "$tmp/eza" /usr/local/bin/eza
  else
    printf '[bootstrap] warning: eza download failed\n' >&2
  fi
  rm -rf "$tmp"
}

download_fastfetch_task() {
  if command -v fastfetch >/dev/null 2>&1; then
    return 0
  fi
  printf '[bootstrap] downloading fastfetch\n'
  fetch https://github.com/fastfetch-cli/fastfetch/releases/latest/download/fastfetch-linux-amd64.deb /tmp/fastfetch.deb \
    || { printf '[bootstrap] warning: fastfetch download failed\n' >&2; return 0; }
}

install_fastfetch_deb() {
  if [[ -f /tmp/fastfetch.deb ]]; then
    apt_get install -y /tmp/fastfetch.deb || printf '[bootstrap] warning: fastfetch install failed\n' >&2
    rm -f /tmp/fastfetch.deb
  fi
}

install_nvim_task() {
  local needs_nvim=0
  if ! command -v nvim >/dev/null 2>&1; then
    needs_nvim=1
  else
    local nvim_line nvim_minor
    nvim_line=$(nvim --version 2>/dev/null | sed -n '1p')
    nvim_minor=$(printf '%s\n' "$nvim_line" | sed -n 's/.*v0\.\([0-9][0-9]*\).*/\1/p')
    if [[ -z $nvim_minor || $nvim_minor -lt 10 ]]; then
      needs_nvim=1
    fi
  fi
  if [[ $needs_nvim -eq 0 ]]; then
    printf '[bootstrap] Neovim is already current: %s\n' "$(nvim --version | head -n 1)"
    return 0
  fi
  printf '[bootstrap] installing current neovim\n'
  local tmp
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
}

install_omz_task() {
  if [[ ! -d /opt/oh-my-zsh/.git ]]; then
    wait_for_command git 60 || { printf 'ERROR: git is unavailable for Oh My Zsh.\n' >&2; return 1; }
    printf '[bootstrap] installing Oh My Zsh\n'
    rm -rf /opt/oh-my-zsh
    git clone --depth 1 https://github.com/ohmyzsh/ohmyzsh.git /opt/oh-my-zsh \
      || { printf 'ERROR: Oh My Zsh clone failed.\n' >&2; return 1; }
  fi
  chmod -R a+rX /opt/oh-my-zsh
}

install_agy_task() {
  if [[ $AGY_INSTALL != 1 ]] || command -v agy >/dev/null 2>&1; then
    return 0
  fi
  printf '[bootstrap] installing Antigravity CLI\n'
  local tmp
  tmp=$(mktemp -d)
  if fetch https://antigravity.google/cli/install.sh "$tmp/install.sh"; then
    bash "$tmp/install.sh" || printf '[bootstrap] warning: Antigravity installer failed\n' >&2
  else
    printf '[bootstrap] warning: Antigravity installer download failed\n' >&2
  fi
  rm -rf "$tmp"
  if [[ -x /root/.local/bin/agy ]]; then
    ln -sfn /root/.local/bin/agy /usr/local/bin/agy
  fi
}

gh_fallback_task() {
  if command -v gh >/dev/null 2>&1; then
    return 0
  fi
  local gh_deb_url
  gh_deb_url=$(curl -fsSL --retry 3 https://api.github.com/repos/cli/cli/releases/latest 2>/dev/null \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(a["browser_download_url"] for a in d["assets"] if a["name"].endswith("linux_amd64.deb")))' 2>/dev/null || true)
  if [[ -n $gh_deb_url ]]; then
    fetch "$gh_deb_url" /tmp/gh.deb && apt_get install -y /tmp/gh.deb || true
    rm -f /tmp/gh.deb
  fi
}

nvim_cache_key() {
  command -v nvim >/dev/null 2>&1 || return 1
  local version lock
  version=$(nvim --version 2>/dev/null | sed -n '1s/^NVIM v//p')
  [[ -n $version ]] || return 1
  lock=$(sha256sum "$PAYLOAD_DIR/config/nvim/lazy-lock.json" 2>/dev/null | awk '{print $1}')
  [[ -n $lock ]] || return 1
  printf 'nvim-v%s-%s\n' "$version" "$lock"
}

install_nvim_plugins_task() {
  command -v nvim >/dev/null 2>&1 || { printf '[bootstrap] warning: Neovim is unavailable; skipping plugins\n' >&2; return 0; }
  wait_for_command git 60 || { printf '[bootstrap] warning: git is unavailable; skipping Neovim plugins\n' >&2; return 0; }
  local key
  key=$(nvim_cache_key) || key=
  if [[ $FORCE -eq 0 && -n $key && -f $NVIM_CACHE_MARKER && "$(cat "$NVIM_CACHE_MARKER" 2>/dev/null || true)" == "$key" ]]; then
    printf '[bootstrap] restored Neovim plugin data is current; skipping plugin rebuild\n'
    return 0
  fi
  if [[ -f $NVIM_CACHE_MARKER ]]; then
    printf '[bootstrap] Neovim plugin cache does not match this runtime; rebuilding plugin data\n'
    rm -rf /root/.local/share/nvim /root/.local/state/nvim /root/.cache/nvim
    rm -f -- "$NVIM_CACHE_MARKER"
  fi
  printf '[bootstrap] restoring Neovim plugins (best effort)\n'
  NVIM_LOG="$STATE_DIR/nvim-bootstrap.log"
  local rc=0
  nvim --headless -c 'Lazy! restore' -c 'qa' >>"$NVIM_LOG" 2>&1 || rc=1
  nvim --headless -c 'TSInstallSync! bash python json yaml javascript typescript' -c 'qa' >>"$NVIM_LOG" 2>&1 || rc=1
  if [[ $rc -eq 0 && -n $key ]]; then
    printf '%s\n' "$key" > "$NVIM_CACHE_MARKER"
    chmod 0600 "$NVIM_CACHE_MARKER"
  fi
  printf '[bootstrap] Neovim plugin setup finished\n'
  return 0
}

validate_runtime() {
  tool_version() {
    case "$1" in
      tmux) tmux -V 2>/dev/null | head -n 1 ;;
      *) "$1" --version 2>/dev/null | head -n 1 ;;
    esac
  }
  local MISSING=()
  local binary version
  for binary in zsh git gh nvim tmux starship eza codex claude opencode node npm python3 cmake; do
    if command -v "$binary" >/dev/null 2>&1; then
      version=$(tool_version "$binary" || true)
      if [[ -z $version ]]; then
        MISSING+=("$binary (does not run)")
      else
        printf '  %-10s %s\n' "$binary" "$version"
      fi
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
  if command -v nvim >/dev/null 2>&1; then
    if ! nvim --headless -c 'qa' >>"$STATE_DIR/nvim-bootstrap.log" 2>&1; then
      printf '[bootstrap] warning: Neovim reported an error while loading the shared configuration\n' >&2
    fi
  fi
  if [[ ${#MISSING[@]} -gt 0 ]]; then
    printf 'ERROR: required tools are missing: %s\n' "${MISSING[*]}" >&2
    return 1
  fi
}

TASK_NAMES=()
TASK_PIDS=()
start_task() {
  local name=$1
  shift
  "$@" &
  TASK_PIDS+=("$!")
  TASK_NAMES+=("$name")
}
wait_task() {
  local name=$1
  local i pid= rc=0
  for i in "${!TASK_NAMES[@]}"; do
    if [[ ${TASK_NAMES[$i]} == "$name" ]]; then
      pid=${TASK_PIDS[$i]}
      break
    fi
  done
  [[ -n $pid ]] || { printf 'ERROR: unknown bootstrap task: %s\n' "$name" >&2; return 1; }
  wait "$pid" || rc=$?
  if [[ $rc -ne 0 ]]; then
    printf '[bootstrap] task %s failed (status %s)\n' "$name" "$rc" >&2
  fi
  return "$rc"
}

install_configuration
milestone config

# Start every independent source at once. apt is the only task that holds the
# dpkg lock; fastfetch and the gh fallback install after apt has completed.
start_task apt install_apt_task
start_task node install_node_task
start_task nvim install_nvim_task
start_task starship install_starship_task
start_task omz install_omz_task
start_task fastfetch download_fastfetch_task
start_task agy install_agy_task

wait_task node || { printf 'ERROR: Node.js installation failed.\n' >&2; exit 1; }
milestone node
start_task npm install_npm_tools_task

wait_task nvim || true
milestone nvim-binary
start_task nvim-plugins install_nvim_plugins_task

wait_task apt || { printf 'ERROR: apt package installation failed.\n' >&2; exit 1; }
milestone apt
set_default_shell
if ! command -v fd >/dev/null 2>&1 && command -v fdfind >/dev/null 2>&1; then
  ln -sfn "$(command -v fdfind)" /usr/local/bin/fd
fi
install_fastfetch_deb
gh_fallback_task
install_eza_task

wait_task npm || { printf 'ERROR: agent CLI installation failed.\n' >&2; exit 1; }
milestone npm
wait_task omz || { printf 'ERROR: Oh My Zsh installation failed.\n' >&2; exit 1; }
wait_task starship || true
wait_task fastfetch || true
wait_task agy || true
wait_task nvim-plugins || true
milestone tasks

validate_runtime || exit 1
milestone validation

printf '[bootstrap] recording completion marker\n'
tmp=$(mktemp "$STATE_DIR/.version.XXXXXX")
printf '%s\n' "$VERSION" > "$tmp"
chmod 0600 "$tmp"
mv -f -- "$tmp" "$MARKER"
date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/bootstrap-completed"
chmod 0600 "$STATE_DIR/bootstrap-completed"
printf '[bootstrap] complete: %s (total %ss)\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(( $(date +%s) - BOOT_START ))"
