#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [ -f compose.generated.yaml ]; then
    echo "==> Stopping runner containers..."
    docker compose -f compose.generated.yaml down
    echo " Runners stopped."
else
    echo "compose.generated.yaml not found. No runners seem to be running."
fi
