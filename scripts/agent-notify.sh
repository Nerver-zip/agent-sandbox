#!/usr/bin/env bash
# agent-notify <claude|codex|agy> [json-payload]
# Notifies the SSH client's desktop when an agent turn ends, with or without tmux.
# The container has no display or D-Bus, so the notification is an OSC escape
# sequence that the client terminal turns into a native desktop notification.
# Inside tmux the sequence is wrapped in a DCS passthrough (allow-passthrough on).
# A BEL is also sent so tmux marks the window in the status bar.
# Payload comes from stdin (Claude Code, agy) or the last argv (Codex `notify`).
# Always exits 0 and never writes to stdout, so it cannot break hook contracts.
#
# AGENT_NOTIFY_OSC selects the protocol understood by the client terminal:
#   99 (default; kitty), 777 (foot, Ghostty, WezTerm, Konsole), 9 (iTerm2,
#   WezTerm, Windows Terminal), off (BEL and tmux message only).

provider="${1:-agent}"; shift
payload=""
if [ $# -gt 0 ]; then payload="${!#}"; elif [ ! -t 0 ]; then payload="$(cat)"; fi

case "$provider" in
  claude) title="Claude Code" ;;
  codex)  title="Codex" ;;
  agy)    title="Antigravity" ;;
  *)      title="$provider" ;;
esac

# Parse payload -> "skip<TAB>cwd<TAB>message"
parsed="$(PAYLOAD="$payload" PROVIDER="$provider" python3 - <<'PY' 2>/dev/null
import json, os
skip, cwd, msg = "0", "", ""
try:
    d = json.loads(os.environ.get("PAYLOAD") or "{}")
    p = os.environ["PROVIDER"]
    if p == "agy":
        # Only normal completions and errors; ignore cancels and continuation limits.
        r = d.get("terminationReason", "NO_TOOL_CALL")
        if r not in ("NO_TOOL_CALL", "ERROR"): skip = "1"
        if r == "ERROR": msg = "Error: " + str(d.get("error", ""))
        cwd = (d.get("workspacePaths") or [""])[0]
        msg = msg or d.get("lastUserInput", "")
    elif p == "codex":
        if d.get("type", "agent-turn-complete") != "agent-turn-complete": skip = "1"
        cwd = d.get("cwd", "")
        msg = d.get("last-assistant-message") or d.get("last_assistant_message") or ""
    else:
        cwd = d.get("cwd", "")
        msg = d.get("last_assistant_message", "")
except Exception:
    pass
# Strip control characters so the message cannot inject terminal sequences.
msg = "".join(c if c.isprintable() else " " for c in str(msg))
msg = " ".join(msg.replace(";", ",").split())
if len(msg) > 140: msg = msg[:137] + "..."
print(f"{skip}\t{cwd}\t{msg}")
PY
)"
IFS=$'\t' read -r skip cwd msg <<<"$parsed"
[ "$skip" = "1" ] && exit 0

proj="$(basename "${cwd:-$PWD}" | tr -d '[:cntrl:];')"
where=""
tty=""
if [ -n "${TMUX:-}" ] && [ -n "${TMUX_PANE:-}" ]; then
  where="$(tmux display-message -p -t "$TMUX_PANE" '#S:#I.#P' 2>/dev/null)"
  tty="$(tmux display-message -p -t "$TMUX_PANE" '#{pane_tty}' 2>/dev/null)"
fi
body="$proj${where:+ [tmux $where]}: ${msg:-Turn finished}"

# Pick a writable terminal: the pane tty under tmux, else the controlling terminal.
[ -w "$tty" ] || tty=/dev/tty
[ -w "$tty" ] || exit 0

emit() {
  if [ -n "${TMUX:-}" ]; then
    # tmux DCS passthrough: ESC characters inside the payload must be doubled.
    printf '\033Ptmux;%s\033\\' "${1//$'\033'/$'\033\033'}" >"$tty" 2>/dev/null
  else
    printf '%s' "$1" >"$tty" 2>/dev/null
  fi
}

case "${AGENT_NOTIFY_OSC:-99}" in
  777) emit "$(printf '\033]777;notify;%s;%s\007' "$title" "$body")" ;;
  9)   emit "$(printf '\033]9;%s: %s\007' "$title" "$body")" ;;
  99)  emit "$(printf '\033]99;i=1:d=0;%s\033\\\033]99;i=1:p=body;%s\033\\' "$title" "$body")" ;;
  off) ;;
esac

# BEL marks the tmux window in the status bar; display-message shows it on the pane.
printf '\a' >"$tty" 2>/dev/null
[ -n "${TMUX:-}" ] && tmux display-message -d 4000 -t "$TMUX_PANE" "$title: turn finished ($proj)" 2>/dev/null
exit 0
