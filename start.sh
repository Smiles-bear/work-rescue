#!/bin/sh
# Run in the user's existing terminal, preserving arguments and the caller's cwd.
set -eu
launcher_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd -P)
if [ -x "$launcher_dir/.venv/bin/python" ]; then
    interpreter="$launcher_dir/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    interpreter=$(command -v python3)
elif command -v python >/dev/null 2>&1; then
    interpreter=$(command -v python)
else
    printf '%s\n' 'Python was not found. Install the environment described in README.md.' >&2
    exit 1
fi
exec "$interpreter" "$launcher_dir/run.py" "$@"

