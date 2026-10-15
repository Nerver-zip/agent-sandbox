#!/usr/bin/env bash
set -euo pipefail
# Do not regenerate the host's identity, and never overwrite mounted tokens.
test -s /etc/ssh/ssh_host_ed25519_key
test -s /etc/ssh/ssh_host_ed25519_key.pub
test -f /home/agent/.codex/auth.json
mkdir -p /run/sshd
chmod 0755 /run/sshd
/usr/sbin/sshd -t -f /etc/ssh/sshd_config
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
