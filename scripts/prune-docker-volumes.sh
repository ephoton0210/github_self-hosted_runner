#!/usr/bin/env bash
# Delete unused anonymous Docker volumes older than N days.
#
#   prune-docker-volumes.sh [days]     default 7
#
# CI jobs start service containers (postgres, ...) whose image declares a VOLUME;
# Docker backs that with an anonymous volume that outlives the container unless
# it was removed with `docker rm -v`. Named volumes are never touched, and a
# volume still referenced by any container (running or stopped) is not "dangling".
# The age guard keeps a volume that was just created but not yet attached.
#
# install.sh copies this file to /etc/cron.daily/docker-volume-prune, so it has
# no dependency on the rest of the repository.
set -euo pipefail

DAYS="${1:-7}"
cutoff=$(date -d "-${DAYS} days" +%s)
removed=0

while read -r name created; do
    [ -n "${name}" ] || continue
    [ "$(date -d "${created}" +%s)" -lt "${cutoff}" ] || continue
    docker volume rm "${name}" >/dev/null 2>&1 && removed=$((removed + 1))
done < <(
    docker volume ls -q -f dangling=true -f label=com.docker.volume.anonymous \
        | xargs -r docker volume inspect --format '{{.Name}} {{.CreatedAt}}'
)

echo "removed ${removed} unused anonymous volume(s) older than ${DAYS} day(s)"
