#!/usr/bin/env bash
set -euo pipefail
# Do not regenerate the host's identity, and never overwrite mounted tokens.
test -s /etc/ssh/ssh_host_ed25519_key
test -s /etc/ssh/ssh_host_ed25519_key.pub
test -f /home/agent/.codex/auth.json
chown -R agent:agent /home/agent/.codex 2>/dev/null || true
test -f /home/agent/.config/git/config
chown -R agent:agent /home/agent/.config/git 2>/dev/null || true
ln -sf /home/agent/.config/git/config /home/agent/.gitconfig 2>/dev/null || true
mkdir -p /run/sshd
chmod 0755 /run/sshd
rm -f /var/run/docker.sock
socat UNIX-LISTEN:/var/run/docker.sock,fork,mode=666,reuseaddr TCP:dind:2375 &
/usr/sbin/sshd -t -f /etc/ssh/sshd_config
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
