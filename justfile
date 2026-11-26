set shell := ["bash", "-uc"]
# Keeps a quoted value such as --frequency "as needed" one argument.
set positional-arguments

default:
    @just --list

setup:
    uv sync --all-groups
    uv run pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg --hook-type pre-push

lint:
    uv run pre-commit run --all-files

test *ARGS:
    uv run pytest tests/ "$@"

# Serve the documentation site locally with live reload.
docs:
    uv run --isolated --only-group docs zensical serve

# Run scripts/schedule.yaml; pushes to HDX. See just sweep --help.
sweep *ARGS:
    ./scripts/sweep.py "$@"

# Load every config the schedule names, as oex would, running nothing.
check:
    ./scripts/sweep.py --check

# Regenerate Tasking Manager configs from the active-projects API without exporting.
tm-configs *ARGS:
    ./scripts/tm_configs.py "$@"

# Regenerate the production configs, then sweep them.
tm:
    ./scripts/tm_configs.py
    ./scripts/sweep.py --group tasking_manager --frequency "as needed"

# The same for sandbox projects.
tm-sandbox:
    ./scripts/tm_configs.py --sandbox --extract
    ./scripts/sweep.py --group tasking_manager_sandbox --frequency "as needed"

# Publish to HDX the geometry a buffered boundary was widened from.
source-geometry CONFIG NAME *ARGS:
    ./scripts/source_geometry.py --config "$1" --name "$2" "${@:3}"
