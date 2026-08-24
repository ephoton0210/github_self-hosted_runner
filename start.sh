#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [ ! -f .env ]; then
    echo "Error: .env file not found." >&2
    echo "Please run: cp .env.example .env and fill in your GH_PAT." >&2
    exit 1
fi

if [ ! -f config/repos.yaml ]; then
    echo "Error: config/repos.yaml not found." >&2
    echo "Please run: cp config/repos.yaml.example config/repos.yaml and configure target repositories." >&2
    exit 1
fi

echo "==> Rendering Docker Compose configuration..."
python3 scripts/render-compose.py

echo "==> Building and starting runner containers..."
docker compose -f compose.generated.yaml up -d --build

echo ""
echo " Runners are up and running!"
echo " - View live logs:    docker compose -f compose.generated.yaml logs -f"
echo " - Check status:      docker compose -f compose.generated.yaml ps"
echo " - Stop runners:      docker compose -f compose.generated.yaml down"
