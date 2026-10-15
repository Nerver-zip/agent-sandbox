#!/usr/bin/env bash
# Host firewall: blocks sandbox egress to host/LAN/Docker/Tailscale.
# Requires Docker with iptables backend and tailscale0 interface.
set -euo pipefail

BRIDGE=br-agentsbx
TAILSCALE_IF=tailscale0
FWD_CHAIN=ASBX_EGRESS
HOST_CHAIN=ASBX_TO_HOST
NET=agent_sandbox_net

err() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || err 'Run as root (sudo).'
command -v iptables >/dev/null || err 'iptables is missing.'
command -v docker >/dev/null || err 'Docker is missing.'
ipt() { iptables -w "$@"; }
has() { iptables -w -C "$@" >/dev/null 2>&1; }

case "${1:-}" in
  install)
    ip link show "$TAILSCALE_IF" >/dev/null 2>&1 || err "Interface $TAILSCALE_IF not found (Tailscale on host)."
    docker network inspect "$NET" >/dev/null 2>&1 || err "Network $NET not found. Run scripts/up.sh."
    actual=$(docker network inspect -f '{{ index .Options "com.docker.network.bridge.name" }}' "$NET")
    [[ "$actual" == "$BRIDGE" ]] || err "Network $NET uses bridge '$actual', expected '$BRIDGE'."
    ipt -S DOCKER-USER >/dev/null 2>&1 || err 'DOCKER-USER chain missing. Native nftables/rootless Docker is unsupported: aborting safely.'
    ipt -N "$FWD_CHAIN" 2>/dev/null || true
    ipt -N "$HOST_CHAIN" 2>/dev/null || true

    # Keep replies to established Tailscale-origin SSH connections working.
    has "$FWD_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN || ipt -A "$FWD_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    has "$HOST_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN || ipt -A "$HOST_CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

    # Private ranges, Docker, tailnet, loopback, link-local, metadata and multicast.
    for cidr in \
      0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
      169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 \
      198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
      has "$FWD_CHAIN" -d "$cidr" -j REJECT || ipt -A "$FWD_CHAIN" -d "$cidr" -j REJECT
    done
    has "$HOST_CHAIN" -j REJECT || ipt -A "$HOST_CHAIN" -j REJECT

    # Block new ingress from other host interfaces/Docker networks.
    has DOCKER-USER -o "$BRIDGE" -m conntrack --ctstate NEW ! -i "$TAILSCALE_IF" -j REJECT || \
      ipt -I DOCKER-USER 1 -o "$BRIDGE" -m conntrack --ctstate NEW ! -i "$TAILSCALE_IF" -j REJECT
    # Block sandbox egress to private ranges. Allow public Internet.
    has DOCKER-USER -i "$BRIDGE" -j "$FWD_CHAIN" || ipt -I DOCKER-USER 1 -i "$BRIDGE" -j "$FWD_CHAIN"
    # DOCKER-USER does not cover packets targeted at the host itself (INPUT).
    has INPUT -i "$BRIDGE" -j "$HOST_CHAIN" || ipt -I INPUT 1 -i "$BRIDGE" -j "$HOST_CHAIN"
    echo 'Firewall active: sandbox -> host/LAN/tailnet/other bridges blocked; public Internet allowed.'
    ;;
  check)
    has DOCKER-USER -i "$BRIDGE" -j "$FWD_CHAIN" || err 'Egress rule missing.'
    has DOCKER-USER -o "$BRIDGE" -m conntrack --ctstate NEW ! -i "$TAILSCALE_IF" -j REJECT || err 'Ingress rule missing.'
    has INPUT -i "$BRIDGE" -j "$HOST_CHAIN" || err 'Host INPUT rule missing.'
    for cidr in 10.0.0.0/8 100.64.0.0/10 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
      has "$FWD_CHAIN" -d "$cidr" -j REJECT || err "CIDR block $cidr is missing."
    done
    has "$HOST_CHAIN" -j REJECT || err 'Host protection rule missing.'
    echo 'Essential firewall rules are present.'
    ;;
  remove)
    while has DOCKER-USER -i "$BRIDGE" -j "$FWD_CHAIN"; do ipt -D DOCKER-USER -i "$BRIDGE" -j "$FWD_CHAIN"; done
    while has DOCKER-USER -o "$BRIDGE" -m conntrack --ctstate NEW ! -i "$TAILSCALE_IF" -j REJECT; do
      ipt -D DOCKER-USER -o "$BRIDGE" -m conntrack --ctstate NEW ! -i "$TAILSCALE_IF" -j REJECT
    done
    while has INPUT -i "$BRIDGE" -j "$HOST_CHAIN"; do ipt -D INPUT -i "$BRIDGE" -j "$HOST_CHAIN"; done
    ipt -F "$FWD_CHAIN" 2>/dev/null || true
    ipt -X "$FWD_CHAIN" 2>/dev/null || true
    ipt -F "$HOST_CHAIN" 2>/dev/null || true
    ipt -X "$HOST_CHAIN" 2>/dev/null || true
    echo 'Agent Sandbox firewall rules removed.'
    ;;
  *) echo "Usage: $0 install|check|remove" >&2; exit 2 ;;
esac
