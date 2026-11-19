#!/bin/bash
# Run the watchdog daemon in the foreground from this script's own directory,
# using the project virtualenv (venv/). Thin wrapper around:
#     venv/bin/python main.py daemon
# (The launchd service does not use this script; it is for manual runs.)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [ ! -x venv/bin/python ]; then
    echo "venv/bin/python not found — create it: python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
exec venv/bin/python main.py daemon "$@"
