#!/usr/bin/env bash
# One-shot setup of a Debian/Ubuntu host for the self-hosted runner fleet.
#
#   ./install.sh               install Docker, create .env and config/repos.yaml, verify
#   ./install.sh --start       the same, then start the runners (./start.sh)
#   ./install.sh --dry-run     print what would be done and change nothing
#
# Safe to run again: every step checks the current state first.
# macOS and Windows hosts do not use this script (see README.md, "Quickstart").
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

START=0
DRY_RUN=0
ASSUME_YES=0
APT_UPDATED=0
NEED_RELOGIN=0
CONFIG_INCOMPLETE=0
SUDO=()

usage() {
    cat <<'EOF'
Usage: ./install.sh [--start] [--yes] [--dry-run] [--help]

Sets up a Debian/Ubuntu host to run the self-hosted runner fleet:
  1. installs Docker Engine, the Compose and Buildx plugins and python3-yaml
  2. enables the Docker service at boot and adds you to the docker group
  3. creates /opt/buildx-cache (bind-mounted into every runner container)
  4. creates .env (asks for GH_PAT) and config/repos.yaml from their examples
  5. checks the result, including that the host clock runs at the right speed

Options:
  --start     run ./start.sh when everything is in place
  --yes       never prompt; GH_PAT is read from the environment when it is set
  --dry-run   print what would be done and change nothing
  --help      show this text

It is safe to run again: every step checks the current state first.
EOF
}

log() { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Run a command, or only print it under --dry-run.
run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

# write_root_file <path> <content>
write_root_file() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] write %s: %s\n' "$1" "$2"
    else
        printf '%s\n' "$2" | "${SUDO[@]}" tee "$1" >/dev/null
    fi
}

in_group() { # in_group <group> [user]: membership as recorded in /etc/group, or of this session without a user
    case " $(id -nG ${2:+"$2"}) " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

apt_update_once() {
    if [ "${APT_UPDATED}" -eq 0 ]; then
        run "${SUDO[@]}" apt-get update
        APT_UPDATED=1
    fi
}

docker_ready() {
    command -v docker >/dev/null 2>&1 \
        && docker compose version >/dev/null 2>&1 \
        && docker buildx version >/dev/null 2>&1
}

preflight() {
    [ "$(uname -s)" = "Linux" ] \
        || die "install.sh supports Linux only. On macOS use ./start-macos.sh, on Windows ./start-windows.ps1 (see README.md)."
    [ -r /etc/os-release ] || die "cannot read /etc/os-release"
    # shellcheck source=/dev/null
    . /etc/os-release
    case "${ID:-}" in
        ubuntu | debian) ;;
        *) die "unsupported distribution '${ID:-unknown}': this script supports Ubuntu and Debian." ;;
    esac
    command -v dpkg >/dev/null 2>&1 || die "dpkg not found"
    case "$(dpkg --print-architecture)" in
        amd64 | arm64) ;;
        *) die "unsupported architecture '$(dpkg --print-architecture)': Docker's apt repository ships amd64 and arm64." ;;
    esac
    if [ "$(id -u)" -ne 0 ]; then
        command -v sudo >/dev/null 2>&1 || die "sudo is required (or run this script as root)."
        SUDO=(sudo)
        if [ "${DRY_RUN}" -eq 0 ]; then
            log "Checking sudo access (you may be asked for your password)..."
            sudo -v
        fi
    fi
}

install_base_packages() {
    local missing=()
    command -v curl >/dev/null 2>&1 || missing+=(curl)
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    python3 -c 'import yaml' >/dev/null 2>&1 || missing+=(python3-yaml)
    dpkg -s ca-certificates >/dev/null 2>&1 || missing+=(ca-certificates)
    if [ "${#missing[@]}" -eq 0 ]; then
        log "Base packages already installed"
        return
    fi
    log "Installing base packages: ${missing[*]}"
    apt_update_once
    run "${SUDO[@]}" apt-get install -y --no-install-recommends "${missing[@]}"
}

# The distribution's own docker.io / docker-compose packages conflict with Docker's packages. Removing them could take
# containers and volumes with it, so stop and let the owner decide.
reject_conflicting_packages() {
    local conflicting=() package
    for package in docker.io docker-compose docker-doc podman-docker; do
        if dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q 'install ok installed'; then
            conflicting+=("${package}")
        fi
    done
    if [ "${#conflicting[@]}" -gt 0 ]; then
        die "conflicting packages are installed: ${conflicting[*]}. Remove them yourself (sudo apt-get remove ${conflicting[*]}) after checking that nothing needs them, then run ./install.sh again."
    fi
}

install_docker() {
    if docker_ready; then
        log "Docker, Compose and Buildx already installed ($(docker --version))"
        return
    fi
    reject_conflicting_packages
    log "Installing Docker Engine from Docker's official apt repository..."
    local codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
    [ -n "${codename}" ] || die "cannot determine the distribution codename from /etc/os-release"
    run "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
    run "${SUDO[@]}" curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    run "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.asc
    write_root_file /etc/apt/sources.list.d/docker.list \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${codename} stable"
    APT_UPDATED=0
    apt_update_once
    run "${SUDO[@]}" apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

enable_docker_service() {
    if [ -d /run/systemd/system ]; then
        log "Enabling the Docker service at boot (the runner containers restart with it)"
        run "${SUDO[@]}" systemctl enable --now docker
    else
        warn "systemd is not running; start the Docker daemon yourself."
    fi
}

ensure_docker_group() {
    local user
    user="$(id -un)"
    if [ "${user}" = "root" ]; then
        return
    fi
    if in_group docker "${user}"; then
        log "'${user}' is already in the docker group"
    else
        log "Adding '${user}' to the docker group"
        run "${SUDO[@]}" usermod -aG docker "${user}"
    fi
    if [ "${DRY_RUN}" -eq 0 ] && ! in_group docker; then
        NEED_RELOGIN=1
    fi
}

ensure_buildx_cache() {
    if [ -d /opt/buildx-cache ]; then
        log "/opt/buildx-cache already exists"
    else
        log "Creating /opt/buildx-cache (shared docker/build-push-action cache, bind-mounted into every runner)"
        run "${SUDO[@]}" install -d -m 0755 /opt/buildx-cache
    fi
}

# set_pat <value>: put the value in .env (kept at mode 600), leaving every other line as it is.
set_pat() {
    local source=.env
    [ -f .env ] || source=.env.example
    (
        umask 077
        PAT="$1" awk 'BEGIN { value = ENVIRON["PAT"] } /^GH_PAT=/ { print "GH_PAT=" value; next } { print }' "${source}" > .env.tmp
        mv .env.tmp .env
    )
}

setup_env_file() {
    if [ -f .env ] && grep -Eq '^GH_PAT=.+' .env; then
        log ".env already holds a GH_PAT"
        run chmod 600 .env
        return
    fi
    [ -f .env.example ] || die ".env.example is missing; is this the repository root?"
    local pat="${GH_PAT:-}"
    if [ -z "${pat}" ] && [ "${ASSUME_YES}" -eq 0 ] && [ "${DRY_RUN}" -eq 0 ] && [ -t 0 ]; then
        printf 'GitHub fine-grained PAT with "Administration: write" (input is hidden, Enter to fill in .env yourself later): '
        read -rs pat
        printf '\n'
    fi
    if [ "${DRY_RUN}" -eq 1 ]; then
        if [ -n "${pat}" ]; then
            printf '[dry-run] create .env from .env.example (mode 600, GH_PAT taken from the environment)\n'
        else
            printf '[dry-run] create .env from .env.example (mode 600), asking for GH_PAT\n'
        fi
        return
    fi
    set_pat "${pat}"
    if [ -z "${pat}" ]; then
        warn ".env was created without a GH_PAT: edit it before starting the runners."
        CONFIG_INCOMPLETE=1
    else
        case "${pat}" in
            github_pat_* | ghp_*) ;;
            *) warn "the PAT does not look like a GitHub token (expected github_pat_... or ghp_...)." ;;
        esac
        log "Wrote .env (mode 600)"
    fi
}

setup_repos_file() {
    if [ ! -f config/repos.yaml ]; then
        if [ "${DRY_RUN}" -eq 1 ]; then
            printf '[dry-run] create config/repos.yaml from config/repos.yaml.example\n'
            return
        fi
        cp config/repos.yaml.example config/repos.yaml
        log "Created config/repos.yaml from the example"
    fi
    if grep -q '<account>' config/repos.yaml; then
        warn "config/repos.yaml still has the example placeholders: set the owner, repo, labels and replicas of your target repositories."
        CONFIG_INCOMPLETE=1
    else
        log "config/repos.yaml is in place"
    fi
}

docker_cli() { # docker with the privileges this session has
    if [ "$(id -u)" -eq 0 ] || in_group docker; then
        docker "$@"
    else
        "${SUDO[@]}" docker "$@"
    fi
}

verify() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '[dry-run] verify the Docker daemon, Compose and Buildx, and render config/repos.yaml\n'
        return
    fi
    log "Verifying the installation..."
    if docker_cli info >/dev/null 2>&1; then
        printf '    docker:  %s (daemon reachable)\n' "$(docker --version)"
    else
        warn "the Docker daemon is not reachable yet; check: sudo systemctl status docker"
    fi
    printf '    compose: %s\n' "$(docker compose version 2>&1 | head -n 1)"
    printf '    buildx:  %s\n' "$(docker buildx version 2>&1 | head -n 1)"
    if [ "${CONFIG_INCOMPLETE}" -eq 0 ]; then
        local rendered
        rendered="$(mktemp)"
        if python3 scripts/render-compose.py config/repos.yaml -o "${rendered}" >/dev/null; then
            printf '    config:  config/repos.yaml renders to %s runner service(s)\n' "$(grep -c 'container_name:' "${rendered}")"
        else
            warn "config/repos.yaml does not render; fix it before starting."
            CONFIG_INCOMPLETE=1
        fi
        rm -f "${rendered}"
    fi
}

# The runner containers share this kernel's clock. A kernel tick other than 10000 makes the clock run fast or slow and
# step back at every NTP poll, which fails tests that order rows by timestamp. Read-only; never fatal.
check_host_clock() {
    python3 - <<'PY' || true
import ctypes


class Timex(ctypes.Structure):
    _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long),
                ("maxerror", ctypes.c_long), ("esterror", ctypes.c_long), ("status", ctypes.c_int),
                ("constant", ctypes.c_long), ("precision", ctypes.c_long), ("tolerance", ctypes.c_long),
                ("time_sec", ctypes.c_long), ("time_usec", ctypes.c_long), ("tick", ctypes.c_long),
                ("pad", ctypes.c_long * 20)]


timex = Timex()  # modes = 0: only reads the kernel clock state
ctypes.CDLL("libc.so.6").adjtimex(ctypes.byref(timex))
if timex.tick == 10000:
    print("    clock:   kernel tick is 10000 (normal)")
else:
    print(f"WARNING: the kernel clock tick is {timex.tick} (normal: 10000). The host clock runs {(timex.tick - 10000) / 100:+.2f}% "
          "off and is stepped back by NTP; timestamp-ordered tests will fail at random on this host.")
PY
}

start_runners() {
    if [ "${CONFIG_INCOMPLETE}" -ne 0 ]; then
        die "not starting: finish .env and config/repos.yaml first (see the warnings above), then run ./start.sh."
    fi
    log "Starting the runners..."
    if [ "$(id -u)" -eq 0 ] || in_group docker; then
        ./start.sh
    else
        sg docker -c "$(printf '%q' "${SCRIPT_DIR}/start.sh")"
    fi
}

summary() {
    printf '\n'
    if [ "${DRY_RUN}" -eq 1 ]; then
        log "Dry run finished: nothing was changed."
        return
    fi
    log "Setup finished."
    if [ "${NEED_RELOGIN}" -eq 1 ]; then
        printf '    Log out and back in (or run: newgrp docker) so that docker works without sudo.\n'
    fi
    if [ "${CONFIG_INCOMPLETE}" -ne 0 ]; then
        printf '    Finish .env and config/repos.yaml, then start the runners with: ./start.sh\n'
    elif [ "${START}" -eq 0 ]; then
        printf '    Start the runners with: ./start.sh   (stop them with: ./stop.sh)\n'
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --start) START=1 ;;
        --yes | -y) ASSUME_YES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --help | -h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown option: $1"
            ;;
    esac
    shift
done

preflight
install_base_packages
install_docker
enable_docker_service
ensure_docker_group
ensure_buildx_cache
setup_env_file
setup_repos_file
verify
check_host_clock
if [ "${START}" -eq 1 ] && [ "${DRY_RUN}" -eq 0 ]; then
    start_runners
fi
summary
