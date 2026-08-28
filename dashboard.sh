#!/usr/bin/env bash
# Start the local status dashboard for this host's runner fleet. Works on
# Linux and macOS; see dashboard.ps1 for Windows.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 is required. Install it, then run: python3 -m pip install -r requirements.txt" >&2
    exit 1
fi

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
    echo "Error: PyYAML is required. Run: python3 -m pip install -r requirements.txt" >&2
    exit 1
fi

exec python3 scripts/dashboard.py "$@"
