export LANG=C.UTF-8
export LC_ALL=C.UTF-8
if ! infocmp "${TERM:-}" >/dev/null 2>&1; then
  export TERM=xterm-256color
fi
# Agent Sandbox shell defaults. Installed inside the image; never mounted from the host.
export PATH="$HOME/.local/bin:$HOME/.local/bin/scripts:$HOME/.npm-global/bin:/usr/local/bin:$PATH"
export EDITOR=nvim VISUAL=nvim
export CODEX_HOME="$HOME/.codex"
export STARSHIP_CONFIG="$HOME/.config/starship/starship.toml"
export ZSH=/opt/oh-my-zsh
ZSH_THEME=""   # Starship provides the prompt.
zstyle ':omz:update' mode disabled
plugins=(git) # no sudo plugin: agent has no sudo privileges
source "$ZSH/oh-my-zsh.sh"

# Debian-provided plugin paths.
[[ -f /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh ]] && source /usr/share/zsh-autosuggestions/zsh-autosuggestions.zsh
[[ -f /usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh ]] && source /usr/share/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh

alias ls='eza --icons --color=auto --group-directories-first'
alias ll='eza -l --icons --color=auto --group-directories-first'
alias la='eza -la --icons --color=auto --group-directories-first'
alias v='nvim'

if [[ -o interactive ]]; then
  eval "$(starship init zsh)"
  # Fastfetch once for each interactive login shell (disable with AGENT_NO_FASTFETCH=1).
  if [[ -o login && -t 1 && ${AGENT_NO_FASTFETCH:-0} != 1 ]]; then
    fastfetch || true
  fi
fi
