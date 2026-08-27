#!/usr/bin/env bash
# Render and load native, ephemeral macOS runner agents for configured repos.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Error: start-macos.sh must be run on a macOS host." >&2
    exit 1
fi

if [ ! -f .env ]; then
    echo "Error: .env file not found." >&2
    echo "Please run: cp .env.example .env and fill in your GH_PAT." >&2
    exit 1
fi

if [ ! -f config/repos.yaml ]; then
    echo "Error: config/repos.yaml not found." >&2
    echo "Please copy config/repos.yaml.example and configure a macos: section." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "Error: Python 3 is required. Install it, then run: python3 -m pip install -r requirements.txt" >&2
    exit 1
fi

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
    echo "Error: PyYAML is required. Run: python3 -m pip install -r requirements.txt" >&2
    exit 1
fi

for command in curl jq launchctl shasum tar; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "Error: required command not found: ${command}" >&2
        exit 1
    fi
done

RUNNER_VERSION="2.336.0"

unload_agent() {
    local label="$1"
    local attempt

    if launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; then
        launchctl bootout "gui/${uid}/${label}"
    fi

    for ((attempt = 1; attempt <= 30; attempt++)); do
        if ! launchctl print "gui/${uid}/${label}" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done

    echo "Error: launchd did not unload ${label} within 30 seconds." >&2
    return 1
}

# A start after removing a macos: section must also unload the old agent. Read
# labels before rendering, so a malformed/new config cannot stop a working fleet.
uid="$(id -u)"
old_manifest=".runner-macos/launchd/manifest.txt"
old_labels=()
if [ -f "${old_manifest}" ]; then
    while IFS= read -r label; do
        if [[ "${label}" =~ ^com\.github-self-hosted-runner\.[A-Za-z0-9.-]+$ ]]; then
            old_labels+=("${label}")
        fi
    done < "${old_manifest}"
fi

echo "==> Rendering macOS launchd configuration..."
python3 scripts/render-macos-launchd.py --runner-version "${RUNNER_VERSION}"

mkdir -p .runner-macos/logs
launchagents_dir="${HOME}/Library/LaunchAgents"
mkdir -p "${launchagents_dir}"
if [ -n "${old_labels[0]:-}" ]; then
    for label in "${old_labels[@]}"; do
        unload_agent "${label}"
        rm -f -- "${launchagents_dir}/${label}.plist"
    done
fi

shopt -s nullglob
plists=(.runner-macos/launchd/*.plist)
if [ "${#plists[@]}" -eq 0 ]; then
    echo "Error: no macOS launchd agents were rendered." >&2
    exit 1
fi

echo "==> Loading native macOS runner agents..."
for plist in "${plists[@]}"; do
    label="$(basename "${plist}" .plist)"
    installed_plist="${launchagents_dir}/${label}.plist"
    unload_agent "${label}"
    install -m 600 "${plist}" "${installed_plist}"
    launchctl bootstrap "gui/${uid}" "${installed_plist}"
done

echo ""
echo "macOS runners are loaded."
echo "- View logs:       tail -f .runner-macos/logs/*.log"
echo "- Check status:    launchctl print gui/${uid}/com.github-self-hosted-runner.<runner>"
echo "- Stop runners:    ./stop-macos.sh"
