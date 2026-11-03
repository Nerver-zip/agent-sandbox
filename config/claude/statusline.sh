#!/usr/bin/env bash
set -u

# Claude Code supplies the JSON session state on standard input.
if ! command -v jq >/dev/null 2>&1; then
  printf 'Claude | install jq for statusline\n'
  exit 0
fi

data=$(cat)
field() { jq -r "$1 // empty" <<<"$data" 2>/dev/null; }

model=$(field '.model.display_name')
effort=$(field '.effort.level')
work_dir=$(field '.workspace.current_dir')
context=$(field '.context_window.remaining_percentage')
five_hour=$(field '.rate_limits.five_hour.used_percentage')
seven_day=$(field '.rate_limits.seven_day.used_percentage')

model=${model:-Claude}
effort=${effort:-auto}
work_dir=${work_dir:-?}
directory=${work_dir##*/}
directory=${directory:-/}

branch=''
if [[ -d "$work_dir" ]] && command -v git >/dev/null 2>&1; then
  branch=$(git -C "$work_dir" branch --show-current 2>/dev/null || true)
fi

# Catppuccin Mocha accents (ANSI true-color).
purple=$'\033[38;2;203;166;247m'
blue=$'\033[38;2;137;180;250m'
green=$'\033[38;2;166;227;161m'
muted=$'\033[38;2;166;173;200m'
reset=$'\033[0m'

printf '%s%s [%s]%s' "$purple" "$model" "$effort" "$reset"
printf '%s | %s%s%s' "$muted" "$blue" "$directory" "$reset"

if [[ -n "$branch" ]]; then
  printf '%s | %s%s%s' "$muted" "$green" "$branch" "$reset"
fi

if [[ -n "$context" ]]; then
  printf '%s | ctx %s%%' "$muted" "${context%.*}"
fi

if [[ -n "$five_hour" ]]; then
  printf ' | 5h %s%%' "${five_hour%.*}"
fi

if [[ -n "$seven_day" ]]; then
  printf ' | 7d %s%%' "${seven_day%.*}"
fi

printf '%s\n' "$reset"
