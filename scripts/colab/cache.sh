#!/usr/bin/env bash
# shellcheck disable=SC2016
# Inspect, prune, invalidate, or clear the local Colab environment cache.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/colab/_common.sh
source "$SCRIPT_DIR/_common.sh"

# Cache maintenance does not need the Colab CLI or Google authentication.
if [[ -f $COLAB_CONFIG ]]; then
  # shellcheck disable=SC1090
  source "$COLAB_CONFIG"
fi
: "${COLAB_CACHE:=0}"
: "${COLAB_CACHE_KEEP:=$COLAB_CACHE_KEEP_DEFAULT}"
: "${COLAB_CACHE_MAX_MB:=$COLAB_CACHE_MAX_MB_DEFAULT}"
: "${COLAB_CACHE_COMPONENTS:=nvim-plugins}"

usage() {
  cat <<'TEXT'
Usage: ./scripts/colab/cache.sh [command]

Commands:
  status                  Show cache artifacts, sizes, and configured limits
  list                    List every artifact with its profile and timestamp
  prune                   Enforce the keep-count and size limits
  invalidate COMPONENT    Remove all artifacts for one component
  clear                   Remove the complete local environment cache

The cache stores only reusable tool data. Workspaces, agent credentials, SSH
keys, and shell history are never captured. Cache corruption or incompatibility
always falls back to the normal bootstrap.
TEXT
}

command=${1:-status}
case "$command" in
  -h|--help|help) usage; exit 0 ;;
  status)
    printf 'Cache directory: %s\n' "$COLAB_CACHE_DIR"
    printf 'Format:          v%s\n' "$COLAB_CACHE_FORMAT"
    printf 'Keep:            %s artifact(s) per component\n' "$COLAB_CACHE_KEEP"
    printf 'Size limit:      %s MiB\n' "$COLAB_CACHE_MAX_MB"
    printf 'Components:      %s\n' "${COLAB_CACHE_COMPONENTS:-nvim-plugins}"
    printf '\n'
    cache_summary
    ;;
  list)
    count=$(cache_count_artifacts)
    if [[ $count -eq 0 ]]; then
      printf 'No cached artifacts under %s\n' "$COLAB_CACHE_DIR"
      exit 0
    fi
    find "$(cache_format_dir)" -type f -name '*.meta' -print 2>/dev/null | while IFS= read -r meta; do
      printf '%s\n' "$meta"
      printf '  component:   %s\n' "$(cache_meta_get "$meta" component || printf unknown)"
      printf '  cache_id:    %s\n' "$(cache_meta_get "$meta" cache_id || printf unknown)"
      printf '  profile:     %s\n' "$(cache_meta_get "$meta" profile || printf unknown)"
      printf '  size:        %s\n' "$(human_bytes "$(cache_meta_get "$meta" size || printf 0)")"
      printf '  created:     %s\n' "$(cache_meta_get "$meta" created || printf unknown)"
      printf '  sha256:      %s\n' "$(cache_meta_get "$meta" sha256 || printf unknown)"
    done
    ;;
  prune)
    cache_prune
    log 'Cache pruned.'
    cache_summary
    ;;
  invalidate)
    component=${2:-}
    [[ -n $component ]] || die 'Usage: cache.sh invalidate COMPONENT'
    [[ $component =~ ^[A-Za-z0-9._-]+$ ]] || die 'Invalid component name.'
    removed=0
    if [[ -d $COLAB_CACHE_DIR ]]; then
      while IFS= read -r dir; do
        [[ -n $dir ]] || continue
        rm -rf -- "$dir"
        removed=$((removed + 1))
      done < <(find "$COLAB_CACHE_DIR" -type d -name "$component" -print 2>/dev/null)
    fi
    if [[ $removed -eq 0 ]]; then
      log "No cached artifacts for component '$component'."
    else
      log "Invalidated component '$component' in $removed profile(s)."
    fi
    ;;
  clear)
    if [[ -d $COLAB_CACHE_DIR ]]; then
      rm -rf -- "${COLAB_CACHE_DIR:?}"
      log 'Environment cache cleared.'
    else
      log 'Environment cache is already empty.'
    fi
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
