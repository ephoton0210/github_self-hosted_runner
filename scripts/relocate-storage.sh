#!/usr/bin/env bash
# Move the runner host's heavy storage off the root filesystem.
#
#   sudo scripts/relocate-storage.sh [target-dir]     default target: /data
#
# Moves, then leaves the old copies in place as *.old until you delete them:
#   /var/lib/docker      -> <target>/docker      (containers, volumes, buildkit; Docker "data-root")
#   /var/lib/containerd  -> <target>/containerd  (image layers, containerd snapshotter)
#   /opt/buildx-cache    -> <target>/buildx-cache (symlinked back, so compose bind mounts keep working)
#   /srv/ci-evidence, /srv/registry -> <target>/... (symlinked back)
#
# Docker is stopped for the duration, so jobs in flight are lost. The runners
# are ephemeral and `restart: unless-stopped`, so they re-register by themselves.
set -euo pipefail

TARGET="${1:-/data}"

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo $0 $*" >&2; exit 1; }
mountpoint -q "${TARGET}" || { echo "${TARGET} is not a mount point; refusing (would stay on the root fs)" >&2; exit 1; }
command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }

log() { printf '==> %s\n' "$*"; }

need_kb=$(du -sk /var/lib/docker /var/lib/containerd /opt/buildx-cache 2>/dev/null | awk '{s+=$1} END {print s}')
avail_kb=$(df -Pk "${TARGET}" | awk 'NR==2 {print $4}')
[ "${avail_kb}" -gt $((need_kb * 12 / 10)) ] || { echo "not enough space on ${TARGET}" >&2; exit 1; }

# sync_dir <src> <dst>: copy src into dst (resumable), only if src is a real dir.
sync_dir() {
    [ -d "$1" ] && [ ! -L "$1" ] || return 0
    install -d -m "$(stat -c %a "$1")" "$2"
    rsync -aHAXS --numeric-ids --info=progress2 "$1"/ "$2"/
}

# link_back <old> <new>: replace old with a symlink to new, keeping old as old.old.
link_back() {
    if [ -d "$1" ] && [ ! -L "$1" ]; then mv "$1" "$1.old"; fi
    [ -L "$1" ] || ln -s "$2" "$1"
}

log "Stopping Docker"
systemctl stop docker.socket docker containerd

log "Copying Docker data-root"
sync_dir /var/lib/docker "${TARGET}/docker"

log "Copying containerd root"
sync_dir /var/lib/containerd "${TARGET}/containerd"

log "Copying buildx cache"
sync_dir /opt/buildx-cache "${TARGET}/buildx-cache"

log "Preparing evidence/registry directories"
install -d -m 0755 "${TARGET}/ci-evidence" "${TARGET}/registry"
for d in ci-evidence registry; do
    if [ -d "/srv/${d}" ] && [ ! -L "/srv/${d}" ]; then sync_dir "/srv/${d}" "${TARGET}/${d}"; fi
done

log "Pointing Docker at ${TARGET}/docker"
install -d /etc/docker
python3 - "${TARGET}" <<'PY'
import json, os, sys
path = "/etc/docker/daemon.json"
cfg = json.load(open(path)) if os.path.exists(path) else {}
cfg["data-root"] = sys.argv[1] + "/docker"
json.dump(cfg, open(path, "w"), indent=2)
PY

log "Pointing containerd at ${TARGET}/containerd"
install -d /etc/containerd
[ -f /etc/containerd/config.toml ] || containerd config default > /etc/containerd/config.toml
if grep -qE '^root = ' /etc/containerd/config.toml; then
    sed -i -E "s|^root = .*|root = \"${TARGET}/containerd\"|" /etc/containerd/config.toml
else
    sed -i "1i root = \"${TARGET}/containerd\"" /etc/containerd/config.toml
fi

log "Swapping old locations for *.old / symlinks"
[ -d /var/lib/docker ] && mv /var/lib/docker /var/lib/docker.old
[ -d /var/lib/containerd ] && mv /var/lib/containerd /var/lib/containerd.old
link_back /opt/buildx-cache "${TARGET}/buildx-cache"
link_back /srv/ci-evidence "${TARGET}/ci-evidence"
link_back /srv/registry "${TARGET}/registry"

log "Starting Docker"
systemctl start containerd docker

docker info 2>/dev/null | grep -E 'Docker Root Dir'
docker ps --format '{{.Names}}\t{{.Status}}' | grep '^runner-' || true
df -h / "${TARGET}"

cat <<EOF

Done. Once the runners are back and a job has passed, reclaim the old space with:
  sudo rm -rf /var/lib/docker.old /var/lib/containerd.old /opt/buildx-cache.old
EOF
