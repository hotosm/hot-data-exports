#!/bin/bash
set -euo pipefail
cd /mnt/disk/osm-country-exports
export PATH="$HOME/.local/bin:$PATH"
# Waits for a running event export, so the two never share the checkout.
mkdir -p .sweep
exec 9>.sweep/server.lock
flock 9
timeout 120 git pull --ff-only
uv sync
set -a; . ./.env; set +a
exec just daily
