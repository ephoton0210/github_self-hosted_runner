#!/usr/bin/env bash
# Native macOS ephemeral-runner supervisor. A launchd agent invokes one copy
# per configured replica; every iteration expands a clean runner directory,
# processes at most one job, deregisters it, and removes that directory.
set -euo pipefail

readonly API_VERSION="2022-11-28"
readonly RETRY_SECONDS=15

OWNER=""
REPO=""
LABELS=""
RUNNER_NAME=""
STATE_DIR=""
ENV_FILE=""
RUNNER_VERSION="${RUNNER_VERSION:-2.336.0}"
RUNNER_ARCH=""
RUNNER_SHA256=""
CURRENT_RUNNER_DIR=""
RUNNER_PID=""
STOP_REQUESTED=0

usage() {
    cat <<'EOF'
Usage: macos-runner-loop.sh --owner OWNER --repo REPO --labels LABELS \\
  --runner-name NAME --state-dir DIRECTORY --env-file FILE
EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --owner)
            OWNER="${2:-}"
            shift 2
            ;;
        --repo)
            REPO="${2:-}"
            shift 2
            ;;
        --labels)
            LABELS="${2:-}"
            shift 2
            ;;
        --runner-name)
            RUNNER_NAME="${2:-}"
            shift 2
            ;;
        --state-dir)
            STATE_DIR="${2:-}"
            shift 2
            ;;
        --env-file)
            ENV_FILE="${2:-}"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "unknown argument: $1"
            ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || die "this supervisor must run on macOS"
[ -n "${OWNER}" ] || die "--owner is required"
[ -n "${REPO}" ] || die "--repo is required"
[ -n "${LABELS}" ] || die "--labels is required"
[ -n "${RUNNER_NAME}" ] || die "--runner-name is required"
[ -n "${STATE_DIR}" ] || die "--state-dir is required"
[ -f "${ENV_FILE}" ] || die "--env-file must point to the existing .env file"
[[ "${RUNNER_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "RUNNER_VERSION is invalid"

case "$(uname -m)" in
    arm64)
        RUNNER_ARCH="arm64"
        RUNNER_SHA256="8e8839c49b7060b6b2154f4931f815df330c27f167d53ef2239ee3dfce28b079"
        ;;
    x86_64)
        RUNNER_ARCH="x64"
        RUNNER_SHA256="f79c43232761ca495fc18df550bb2865aa99984b37c173c0aa1f8c09d0d548fe"
        ;;
    *)
        die "unsupported macOS architecture: $(uname -m)"
        ;;
esac

if [ "${RUNNER_VERSION}" != "2.336.0" ]; then
    die "RUNNER_VERSION ${RUNNER_VERSION} is not pinned in this release; update the version and SHA-256 together"
fi

read_env_value() {
    local key="$1"
    local line value
    line="$(grep -m 1 "^${key}=" "${ENV_FILE}" || true)"
    [ -n "${line}" ] || die "${key} is missing from ${ENV_FILE}"
    value="${line#*=}"
    value="${value%$'\r'}"
    if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
        value="${value:1:${#value}-2}"
    elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
        value="${value:1:${#value}-2}"
    fi
    [ -n "${value}" ] || die "${key} is empty in ${ENV_FILE}"
    printf '%s' "${value}"
}

GH_PAT="$(read_env_value GH_PAT)"
export GH_PAT
readonly API_BASE="https://api.github.com/repos/${OWNER}/${REPO}"
readonly AUTH_HEADER="Authorization: Bearer ${GH_PAT}"

api_post_token() {
    local endpoint="$1"
    local response http_code body
    if ! response="$(curl --silent --show-error --retry 3 \
        --write-out $'\n%{http_code}' --request POST \
        --header "${AUTH_HEADER}" \
        --header "Accept: application/vnd.github+json" \
        --header "X-GitHub-Api-Version: ${API_VERSION}" \
        "${API_BASE}/actions/runners/${endpoint}")"; then
        echo "Error: could not request ${endpoint} for ${OWNER}/${REPO}." >&2
        return 1
    fi
    http_code="$(printf '%s\n' "${response}" | tail -n 1)"
    body="$(printf '%s\n' "${response}" | sed '$d')"
    if [ "${http_code}" != "201" ] && [ "${http_code}" != "200" ]; then
        echo "Error: GitHub API returned HTTP ${http_code} for ${endpoint}." >&2
        echo "Response: ${body}" >&2
        return 1
    fi
    printf '%s\n' "${body}" | jq -er '.token'
}

remove_current_runner() {
    local removal_token=""
    if [ -n "${CURRENT_RUNNER_DIR}" ] && [ -x "${CURRENT_RUNNER_DIR}/config.sh" ]; then
        removal_token="$(api_post_token remove-token || true)"
        if [ -n "${removal_token}" ]; then
            (
                cd "${CURRENT_RUNNER_DIR}"
                ./config.sh remove --token "${removal_token}" || true
            )
        fi
    fi

    if [ -n "${CURRENT_RUNNER_DIR}" ]; then
        rm -rf -- "${CURRENT_RUNNER_DIR}"
        CURRENT_RUNNER_DIR=""
    fi
}

shutdown() {
    STOP_REQUESTED=1
    if [ -n "${RUNNER_PID}" ] && kill -0 "${RUNNER_PID}" 2>/dev/null; then
        kill -TERM "${RUNNER_PID}" 2>/dev/null || true
        wait "${RUNNER_PID}" || true
    fi
    RUNNER_PID=""
    remove_current_runner
    exit 0
}
trap shutdown INT TERM

mkdir -p "${STATE_DIR}/cache"
ARCHIVE="${STATE_DIR}/cache/actions-runner-osx-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"
if [ -f "${ARCHIVE}" ]; then
    actual_sha256="$(shasum -a 256 "${ARCHIVE}" | awk '{print $1}')"
    if [ "${actual_sha256}" != "${RUNNER_SHA256}" ]; then
        echo "Warning: cached runner archive failed SHA-256 verification; downloading it again." >&2
        rm -f -- "${ARCHIVE}"
    fi
fi
if [ ! -f "${ARCHIVE}" ]; then
    temporary_archive="$(mktemp "${STATE_DIR}/cache/actions-runner.XXXXXX")"
    echo "==> Downloading actions/runner ${RUNNER_VERSION} for macOS ${RUNNER_ARCH}..."
    if ! curl --fail --location --retry 3 --output "${temporary_archive}" \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-osx-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz"; then
        rm -f -- "${temporary_archive}"
        die "runner archive download failed"
    fi
    actual_sha256="$(shasum -a 256 "${temporary_archive}" | awk '{print $1}')"
    if [ "${actual_sha256}" != "${RUNNER_SHA256}" ]; then
        rm -f -- "${temporary_archive}"
        die "runner archive SHA-256 did not match the pinned release"
    fi
    mv "${temporary_archive}" "${ARCHIVE}"
fi

while [ "${STOP_REQUESTED}" -eq 0 ]; do
    CURRENT_RUNNER_DIR="$(mktemp -d "${STATE_DIR}/runner.XXXXXX")"
    if ! tar -xzf "${ARCHIVE}" -C "${CURRENT_RUNNER_DIR}"; then
        echo "Error: could not extract ${ARCHIVE}; retrying in ${RETRY_SECONDS}s." >&2
        remove_current_runner
        sleep "${RETRY_SECONDS}"
        continue
    fi

    registration_token="$(api_post_token registration-token || true)"
    if [ -z "${registration_token}" ]; then
        echo "Error: registration token unavailable; retrying in ${RETRY_SECONDS}s." >&2
        remove_current_runner
        sleep "${RETRY_SECONDS}"
        continue
    fi

    if ! (
        cd "${CURRENT_RUNNER_DIR}"
        ./config.sh --unattended \
            --url "https://github.com/${OWNER}/${REPO}" \
            --token "${registration_token}" \
            --name "${RUNNER_NAME}" \
            --labels "${LABELS}" \
            --work "${CURRENT_RUNNER_DIR}/_work" \
            --ephemeral \
            --disableupdate \
            --replace
    ); then
        echo "Error: runner configuration failed; retrying in ${RETRY_SECONDS}s." >&2
        remove_current_runner
        sleep "${RETRY_SECONDS}"
        continue
    fi

    echo "==> ${RUNNER_NAME} is ready for one job."
    (
        cd "${CURRENT_RUNNER_DIR}"
        ./run.sh
    ) &
    RUNNER_PID=$!
    if wait "${RUNNER_PID}"; then
        runner_exit_code=0
    else
        runner_exit_code=$?
    fi
    RUNNER_PID=""
    remove_current_runner

    if [ "${STOP_REQUESTED}" -ne 0 ]; then
        break
    fi
    if [ "${runner_exit_code}" -ne 0 ]; then
        echo "Warning: ${RUNNER_NAME} exited with ${runner_exit_code}; retrying in ${RETRY_SECONDS}s." >&2
        sleep "${RETRY_SECONDS}"
    fi
done
