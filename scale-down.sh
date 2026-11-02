#!/usr/bin/env bash
# Remove runner containers that are no longer in config/repos.yaml, without
# interrupting running jobs. Busy runners are skipped; re-run later to retry.
#
# Scale up needs no special script: edit replicas, then ./start.sh
# (`up -d` only creates the missing containers).
#
# Usage: ./scale-down.sh [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

dry_run=0
[ "${1:-}" = "--dry-run" ] && dry_run=1

[ -f config/repos.yaml ] || { echo "Error: config/repos.yaml not found." >&2; exit 1; }

python3 scripts/render-compose.py >/dev/null
project="$(docker compose -f compose.generated.yaml config --format json | jq -r .name)"
wanted="$(docker compose -f compose.generated.yaml config --services)"

skipped=0
removed=0
while IFS=$'\t' read -r container service; do
    [ -n "${container}" ] || continue
    if printf '%s\n' "${wanted}" | grep -qxF "${service}"; then
        continue
    fi
    if docker exec "${container}" pgrep -f Runner.Worker >/dev/null 2>&1; then
        echo "BUSY  ${container}: running a job, skipped (retry later)"
        skipped=$((skipped + 1))
        continue
    fi
    if [ "${dry_run}" = 1 ]; then
        echo "WOULD REMOVE ${container}"
    else
        # SIGTERM lets register-runner.sh deregister the runner from GitHub.
        docker stop "${container}" >/dev/null && docker rm "${container}" >/dev/null
        echo "REMOVED ${container}"
    fi
    removed=$((removed + 1))
done < <(docker ps --filter "label=com.docker.compose.project=${project}" \
    --format '{{.Names}}	{{.Label "com.docker.compose.service"}}')

echo "Done: ${removed} removed/removable, ${skipped} busy-skipped."
[ "${skipped}" -eq 0 ] || echo "Re-run ./scale-down.sh once the busy runners finish."
