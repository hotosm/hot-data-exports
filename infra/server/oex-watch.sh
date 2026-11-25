#!/bin/bash
# Wrapped in main so bash parses it all before git merge can change this file.
main() {
    set -euo pipefail
    cd "$(dirname "$(readlink -f "$0")")/../.."
    export PATH="$HOME/.local/bin:$PATH"
    mkdir -p .sweep
    exec 9>.sweep/server.lock
    if ! flock --nonblock 9; then
        echo "another export holds the lock; trying again next tick"
        exit 0
    fi
    timeout 120 git fetch --quiet origin main
    if [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]; then
        exit 0
    fi
    changed=$(git diff --name-only HEAD origin/main)
    git merge --ff-only origin/main
    uv sync --locked
    if ! grep -q '^configs/events/' <<<"$changed"; then
        exit 0
    fi
    set -a; . ./.env; set +a
    exec ./scripts/sweep.py --group events --due
}
main "$@"
