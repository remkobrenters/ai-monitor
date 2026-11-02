#!/bin/sh
set -eu
python=/usr/bin/python3
if [ "$(uname -s)" = Darwin ]; then
    python="$(command -v python3)" || { echo "ai-monitor: Python 3.11+ is required (for example: brew install python)" >&2; exit 1; }
fi
exec "$python" "$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)/scripts/manage.py" uninstall "$@"
