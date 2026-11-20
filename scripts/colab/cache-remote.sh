#!/usr/bin/env bash
# Remote-side helper for the Colab environment cache. This file is uploaded with
# the bootstrap payload and executed over SSH; it is not meant to be sourced.
#
# Cache artifacts contain only the explicitly listed tool paths below. They must
# never contain workspaces, agent credentials, SSH keys, or shell history.
set -Eeuo pipefail
umask 022

CACHE_FORMAT=1
CACHE_ROOT=${CACHE_REMOTE_ROOT:-/}
STATE_DIR=${CACHE_REMOTE_STATE:-/root/.agent-sandbox}
NVIM_CACHE_MARKER="$STATE_DIR/nvim-cache-key"

log() { printf '[cache] %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'TEXT'
Usage: cache-remote.sh <command> [component] [value]

Commands:
  profile                 Print a stable OS/architecture/libc profile
  probe COMPONENT         Print cache metadata for an installed component
  capture COMPONENT       Write a gzip tar archive of the component to stdout
  restore COMPONENT       Read a gzip tar archive from stdin and install it
  validate-archive COMPONENT
                          Validate an archive from stdin without installing it
  clean COMPONENT         Remove the component's cached paths
  mark COMPONENT VALUE    Record that COMPONENT was restored as VALUE
TEXT
}

# Every path is relative to CACHE_ROOT and must be listed here. Keep this list
# free of configuration, credentials, workspaces, and other mutable user state.
component_paths() {
  case "$1" in
    nvim-plugins)
      printf '%s\n' \
        root/.local/share/nvim
      ;;
    shell)
      printf '%s\n' \
        opt/oh-my-zsh \
        usr/local/bin/starship
      ;;
    *)
      die "Unknown cache component: $1"
      ;;
  esac
}

component_exists() {
  local component=$1
  local path
  local found=0
  while IFS= read -r path; do
    if [[ -e "$CACHE_ROOT/$path" || -L "$CACHE_ROOT/$path" ]]; then
      found=1
    fi
  done < <(component_paths "$component")
  [[ $found -eq 1 ]]
}

profile_id() {
  local id version arch libc
  id=$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | head -n 1)
  version=$(sed -n 's/^VERSION_ID=//p' /etc/os-release 2>/dev/null | head -n 1)
  arch=$(uname -m)
  libc=$(ldd --version 2>/dev/null | sed -n '1s/.* //p' | head -n 1)
  id=${id//\"/}
  version=${version//\"/}
  [[ -n $id ]] || id=unknown
  [[ -n $version ]] || version=unknown
  [[ -n $arch ]] || arch=unknown
  printf '%s-%s-%s-glibc%s\n' "$id" "$version" "$arch" "${libc:-unknown}"
}

path_allowed() {
  local component=$1
  local candidate=$2
  local path
  candidate=${candidate#./}
  [[ -n $candidate ]] || return 1
  [[ $candidate != /* ]] || return 1
  case "/$candidate/" in
    */../*) return 1 ;;
  esac
  while IFS= read -r path; do
    if [[ $candidate == "$path" || $candidate == "$path/"* ]]; then
      return 0
    fi
  done < <(component_paths "$component")
  return 1
}

validate_archive_stream() {
  local component=$1
  local tmp entry bad=0
  tmp=$(mktemp "$STATE_DIR/.cache-validate.XXXXXX")
  cat > "$tmp"
  if ! gzip -t "$tmp" 2>/dev/null; then
    rm -f -- "$tmp"
    die 'Cache archive is not valid gzip.'
  fi
  while IFS= read -r entry; do
    entry=${entry#./}
    if ! path_allowed "$component" "$entry"; then
      printf 'ERROR: cache archive contains a path outside the %s allowlist: %s\n' "$component" "$entry" >&2
      bad=1
      break
    fi
  done < <(tar -tzf "$tmp")
  rm -f -- "$tmp"
  [[ $bad -eq 0 ]] || exit 1
}

validate_archive_file() {
  local component=$1
  local archive=$2
  local entry
  gzip -t "$archive" 2>/dev/null || die 'Cache archive is not valid gzip.'
  while IFS= read -r entry; do
    entry=${entry#./}
    if ! path_allowed "$component" "$entry"; then
      die "Cache archive contains a path outside the $component allowlist: $entry"
    fi
  done < <(tar -tzf "$archive")
}

compressor() {
  if command -v pigz >/dev/null 2>&1; then
    printf 'pigz -1'
  else
    printf 'gzip -1'
  fi
}

probe_component() {
  local component=$1
  case "$component" in
    nvim-plugins)
      local version lock count bytes
      command -v nvim >/dev/null 2>&1 || die 'Neovim is not installed.'
      version=$(nvim --version 2>/dev/null | sed -n '1s/^NVIM v//p')
      [[ -n $version ]] || die 'Could not determine the Neovim version.'
      if [[ -f /root/.config/nvim/lazy-lock.json ]]; then
        lock=$(sha256sum /root/.config/nvim/lazy-lock.json | awk '{print $1}')
      else
        lock=missing
      fi
      count=$(find "$CACHE_ROOT/root/.local/share/nvim" -type f 2>/dev/null | wc -l | tr -d ' ')
      bytes=$(du -sb "$CACHE_ROOT/root/.local/share/nvim" 2>/dev/null | awk '{print $1}')
      [[ -n $bytes ]] || bytes=0
      printf 'nvim_version=%s\n' "$version"
      printf 'lazy_lock=%s\n' "$lock"
      printf 'files=%s\n' "$count"
      printf 'bytes=%s\n' "$bytes"
      ;;
    shell)
      local starship_version omz_commit
      starship_version=$(starship --version 2>/dev/null | sed -n '1s/^starship //p')
      [[ -n $starship_version ]] || starship_version=unknown
      omz_commit=$(git -C /opt/oh-my-zsh rev-parse --short HEAD 2>/dev/null || printf unknown)
      printf 'starship_version=%s\n' "$starship_version"
      printf 'omz_commit=%s\n' "$omz_commit"
      ;;
    *)
      die "Unknown cache component: $component"
      ;;
  esac
}

capture_component() {
  local component=$1
  local list tmp path
  component_exists "$component" || die "Nothing to capture for $component."
  # Build the NUL-separated member list explicitly so tar never walks into a
  # directory that is not part of the allowlist.
  list=$(mktemp "$STATE_DIR/.cache-list.XXXXXX")
  while IFS= read -r path; do
    if [[ -e "$CACHE_ROOT/$path" || -L "$CACHE_ROOT/$path" ]]; then
      printf '%s\0' "$path" >> "$list"
    fi
  done < <(component_paths "$component")
  [[ -s $list ]] || { rm -f -- "$list"; die "Nothing to capture for $component."; }
  log "capturing $component"
  # -C CACHE_ROOT keeps the archive relative; --null is safe for every path.
  tar -C "$CACHE_ROOT" --null -T "$list" -I "$(compressor)" -cf -
  rm -f -- "$list"
}

restore_component() {
  local component=$1
  local tmp
  mkdir -p "$STATE_DIR"
  tmp=$(mktemp "$STATE_DIR/.cache-restore.XXXXXX")
  cat > "$tmp"
  validate_archive_file "$component" "$tmp" || { rm -f -- "$tmp"; exit 1; }
  log "installing $component"
  if command -v pigz >/dev/null 2>&1; then
    pigz -dc "$tmp" | tar -xf - -C "$CACHE_ROOT" --no-same-owner
  else
    gzip -dc "$tmp" | tar -xf - -C "$CACHE_ROOT" --no-same-owner
  fi
  rm -f -- "$tmp"
  verify_component "$component"
}

verify_component() {
  local component=$1
  case "$component" in
    nvim-plugins)
      local lazy="$CACHE_ROOT/root/.local/share/nvim/lazy"
      [[ -d $lazy ]] || die 'Restored Neovim plugin directory is missing.'
      local count
      count=$(find "$lazy" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')
      [[ $count -ge 5 ]] || die "Restored Neovim plugin directory is incomplete ($count plugins)."
      if command -v nvim >/dev/null 2>&1; then
        local minor
        minor=$(nvim --version 2>/dev/null | sed -n '1s/.*v0\.\([0-9][0-9]*\).*/\1/p')
        if [[ -n $minor && $minor -ge 10 ]]; then
          local log_file="$STATE_DIR/nvim-cache-verify.log"
          if ! nvim --headless -c 'qa' >>"$log_file" 2>&1; then
            die 'Neovim failed to start with the restored plugin data.'
          fi
        fi
      fi
      ;;
    shell)
      [[ -d "$CACHE_ROOT/opt/oh-my-zsh" ]] || die 'Restored Oh My Zsh directory is missing.'
      if [[ -x "$CACHE_ROOT/usr/local/bin/starship" ]]; then
        "$CACHE_ROOT/usr/local/bin/starship" --version >/dev/null 2>&1 \
          || die 'The restored starship binary does not run.'
      fi
      ;;
    *)
      die "Unknown cache component: $component"
      ;;
  esac
}

clean_component() {
  local component=$1
  local path
  while IFS= read -r path; do
    rm -rf -- "$CACHE_ROOT/$path"
  done < <(component_paths "$component")
  if [[ $component == nvim-plugins ]]; then
    rm -f -- "$NVIM_CACHE_MARKER"
  fi
}

mark_component() {
  local component=$1
  local value=$2
  [[ $component == nvim-plugins ]] || die "mark is not supported for $component."
  mkdir -p "$STATE_DIR"
  printf '%s\n' "$value" > "$NVIM_CACHE_MARKER"
  chmod 0600 "$NVIM_CACHE_MARKER"
}

command=${1:-}
case "$command" in
  profile) profile_id ;;
  probe)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    probe_component "$2"
    ;;
  capture)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    capture_component "$2"
    ;;
  restore)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    restore_component "$2"
    ;;
  validate-archive)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    validate_archive_stream "$2"
    ;;
  clean)
    [[ $# -ge 2 ]] || { usage >&2; exit 2; }
    clean_component "$2"
    ;;
  mark)
    [[ $# -ge 3 ]] || { usage >&2; exit 2; }
    mark_component "$2" "$3"
    ;;
  -h|--help|help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
