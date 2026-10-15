# syntax=docker/dockerfile:1
# Debian 13 packages include eza, fastfetch, starship and a modern Node.js.
FROM debian:trixie-slim

ARG CODEX_VERSION=latest
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    EDITOR=nvim VISUAL=nvim \
    CODEX_HOME=/home/agent/.codex \
    STARSHIP_CONFIG=/home/agent/.config/starship/starship.toml

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash zsh zsh-autosuggestions zsh-syntax-highlighting \
      openssh-server openssh-client ca-certificates curl wget git gh gnupg kitty-terminfo \
      neovim tmux eza fastfetch starship jq ripgrep fd-find fzf less htop tree file procps \
      unzip zip tar xz-utils gzip bash-completion \
      python3 python3-venv python3-pip pipx \
      nodejs npm \
      build-essential pkg-config make cmake \
      dnsutils netcat-openbsd tini \
    && rm -rf /var/lib/apt/lists/* \
    && echo 'export LANG=C.UTF-8\nexport LC_ALL=C.UTF-8' >> /etc/zsh/zshenv \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd \
    && useradd --create-home --uid 10001 --shell /usr/bin/zsh agent \
    && passwd -d agent \
    && install -d -m 0700 -o agent -g agent /home/agent/.ssh /home/agent/.codex \
    && install -d -m 0755 /run/sshd \
    && rm -f /etc/ssh/ssh_host_*key* \
    && mkdir -p /home/agent/workspace /home/agent/.config/starship /opt/agent-secrets \
    && chown -R agent:agent /home/agent

# Build-time CLIs are installed into immutable image layers, not into the disposable workspace.
# CODEX_VERSION can be pinned using --build-arg or via compose.yaml build.args.
RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && command -v codex && codex --version

# Remote Oh My Zsh source (track updates explicitly by rebuilding).
RUN git clone --depth 1 https://github.com/ohmyzsh/ohmyzsh.git /opt/oh-my-zsh \
    && chmod -R a+rX /opt/oh-my-zsh

# Install Antigravity CLI as the unprivileged user at image build time.
# Review upstream installer before trusting it; the installed binary is baked into the image.
RUN curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/agy-install.sh \
    && chmod 0644 /tmp/agy-install.sh \
    && su -s /bin/bash agent -c 'bash /tmp/agy-install.sh' \
    && test -x /home/agent/.local/bin/agy \
    && /home/agent/.local/bin/agy --version \
    && rm -f /tmp/agy-install.sh

COPY --chown=agent:agent --chmod=0600 ssh/authorized_keys /home/agent/.ssh/authorized_keys
COPY --chown=agent:agent --chmod=0644 config/agent.zshrc /home/agent/.zshrc
COPY --chown=agent:agent --chmod=0644 config/starship.toml /home/agent/.config/starship/starship.toml
COPY --chown=agent:agent --chmod=0644 config/codex.toml /home/agent/.codex/config.toml
COPY --chmod=0644 sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 scripts/container-entrypoint.sh /usr/local/sbin/agent-entrypoint
# gh wrapper loads the fine-grained token only for gh processes, not the entire shell.
COPY --chmod=0755 scripts/gh-wrapper.sh /usr/local/bin/gh
# All HTTPS Git operations on github.com use the same scoped gh token when present.
RUN git config --system credential.https://github.com.helper '!/usr/local/bin/gh auth git-credential'


WORKDIR /home/agent/workspace
EXPOSE 2222/tcp
ENTRYPOINT ["/usr/local/sbin/agent-entrypoint"]
