#!/usr/bin/env bash
# Entrypoint for docker/runner/Dockerfile.
#
# Mints a short-lived registration token via the GitHub API (registration
# tokens expire in 1h, so they're never baked in — see
# development/02_DEPLOYMENT_DESIGN.md), registers this container as an
# --ephemeral runner, runs exactly one job, then exits. Compose's restart
# policy starts a fresh container to pick up the next job.
set -euo pipefail

: "${GH_PAT:?GH_PAT (fine-grained PAT, Administration: write) is required}"
: "${GH_OWNER:?GH_OWNER is required}"
: "${GH_REPO:?GH_REPO is required}"
: "${RUNNER_NAME:?RUNNER_NAME is required}"
: "${RUNNER_LABELS:?RUNNER_LABELS is required (comma-separated)}"

API_BASE="https://api.github.com/repos/${GH_OWNER}/${GH_REPO}"
AUTH_HEADER="Authorization: Bearer ${GH_PAT}"

api_post_token() {
    local endpoint="$1"
    local response http_code body
    response="$(curl -s -w "\n%{http_code}" -X POST \
        -H "${AUTH_HEADER}" \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "${API_BASE}/actions/runners/${endpoint}")"
    http_code="$(printf '%s\n' "${response}" | tail -n1)"
    body="$(printf '%s\n' "${response}" | sed '$d')"

    if [ "${http_code}" != "201" ] && [ "${http_code}" != "200" ]; then
        echo "Error: GitHub API returned HTTP ${http_code} when requesting ${endpoint} for ${GH_OWNER}/${GH_REPO}." >&2
        echo "Response: ${body}" >&2
        echo "Please verify that GH_PAT has 'Administration: write' permissions and repo access for ${GH_REPO}." >&2
        return 1
    fi
    printf '%s\n' "${body}" | jq -r .token
}

cleanup() {
    local removal_token
    removal_token="$(api_post_token remove-token || true)"
    if [ -n "${removal_token:-}" ]; then
        ./config.sh remove --token "${removal_token}" || true
    fi
}
trap cleanup EXIT INT TERM

registration_token="$(api_post_token registration-token)"
if [ -z "${registration_token}" ] || [ "${registration_token}" = "null" ]; then
    echo "failed to mint a registration token for ${GH_OWNER}/${GH_REPO}" >&2
    exit 1
fi

./config.sh \
    --unattended \
    --url "https://github.com/${GH_OWNER}/${GH_REPO}" \
    --token "${registration_token}" \
    --name "${RUNNER_NAME}" \
    --labels "${RUNNER_LABELS}" \
    --ephemeral \
    --replace

./run.sh
