#!/usr/bin/env bash
# Administrator-only: apt-installed packages exist only until down/recreate.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $# -gt 0 ]] || { echo 'Usage: scripts/install-packages.sh <apt-package> ...' >&2; exit 2; }
for pkg in "$@"; do
  [[ "$pkg" =~ ^[a-zA-Z0-9][a-zA-Z0-9.+-]*$ ]] || { echo "Invalid package name: $pkg" >&2; exit 2; }
done
docker compose exec -T --user root workspace apt-get update
docker compose exec -T --user root workspace apt-get install -y --no-install-recommends "$@"
echo 'Packages installed in the current disposable container only.'
