#!/usr/bin/env bash
# Delete CI evidence older than N days from the local artifact store.
#
#   prune-ci-evidence.sh [days]     default 14, directory from CI_EVIDENCE_DIR
#
# install.sh copies this file to /etc/cron.daily/ci-evidence-prune, so it has
# no dependency on the rest of the repository.
set -euo pipefail

DAYS="${1:-14}"
DIR="${CI_EVIDENCE_DIR:-/srv/ci-evidence}"

[ -d "${DIR}" ] || exit 0

# Each run writes into <DIR>/<run_id>/...; age is judged per top-level entry.
find "${DIR}" -mindepth 1 -maxdepth 1 -mtime "+${DAYS}" -exec rm -rf -- {} +
