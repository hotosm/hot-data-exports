#!/bin/bash
# Wrapped in main so bash parses it all before git pull can change this file.
main() {
    set -euo pipefail
    cd "$(dirname "$(readlink -f "$0")")/../.."
    export PATH="$HOME/.local/bin:$PATH"
    # Waits for a running event export, so the two never share the checkout.
    mkdir -p .sweep
    exec 9>.sweep/server.lock
    flock 9
    timeout 120 git pull --ff-only
    uv sync --locked
    set -a; . ./.env; set +a
    exec ./scripts/sweep.py --due
}
main "$@"
