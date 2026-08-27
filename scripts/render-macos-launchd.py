#!/usr/bin/env python3
"""Render per-runner launchd agents from the optional ``macos`` repo config."""

from __future__ import annotations

import argparse
import plistlib
import re
from pathlib import Path

import yaml


LABEL_PREFIX = "com.github-self-hosted-runner"
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")


def fail(message: str) -> None:
    raise ValueError(message)


def require_string(entry: dict, field: str, context: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        fail(f"{context}.{field} must be a non-empty string")
    return value


def require_labels(value: object, context: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(label, str) and label for label in value
    ):
        fail(f"{context}.labels must be a non-empty list of strings")
    return value


def require_replicas(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        fail(f"{context}.replicas must be a positive integer")
    return value


def service_name(repo: str, replica: int) -> str:
    name = f"runner-{repo.lower()}-macos-{replica}"
    if not SERVICE_NAME_RE.fullmatch(name):
        fail(
            f"repo {repo!r} produces unsupported launchd service name {name!r}; "
            "use a GitHub repository name containing only letters, digits, dots, "
            "and hyphens"
        )
    return name


def render(
    repos_config: dict,
    project_root: Path,
    output_dir: Path,
    runner_version: str,
) -> list[tuple[str, dict]]:
    if not isinstance(repos_config, dict) or not isinstance(repos_config.get("repos"), list):
        fail("top-level 'repos' must be a list")

    script_path = project_root / "scripts" / "macos-runner-loop.sh"
    env_path = project_root / ".env"
    state_root = project_root / ".runner-macos"
    rendered: list[tuple[str, dict]] = []

    for repo_index, entry in enumerate(repos_config["repos"], start=1):
        context = f"repos[{repo_index}]"
        if not isinstance(entry, dict):
            fail(f"{context} must be a mapping")

        macos = entry.get("macos")
        if macos is None:
            continue
        if not isinstance(macos, dict):
            fail(f"{context}.macos must be a mapping")

        owner = require_string(entry, "owner", context)
        repo = require_string(entry, "repo", context)
        labels = require_labels(macos.get("labels", ["macos"]), f"{context}.macos")
        replicas = require_replicas(macos.get("replicas", 1), f"{context}.macos")
        runner_name_prefix = macos.get("runner_name_prefix", f"runner-{repo.lower()}-macos")
        if not isinstance(runner_name_prefix, str) or not runner_name_prefix:
            fail(f"{context}.macos.runner_name_prefix must be a non-empty string")

        for replica in range(1, replicas + 1):
            name = service_name(repo, replica)
            label = f"{LABEL_PREFIX}.{name}"
            slot_dir = state_root / "slots" / name
            log_path = state_root / "logs" / f"{name}.log"
            plist = {
                "Label": label,
                "ProgramArguments": [
                    "/bin/bash",
                    str(script_path),
                    "--owner",
                    owner,
                    "--repo",
                    repo,
                    "--labels",
                    ",".join(labels),
                    "--runner-name",
                    f"{runner_name_prefix}-{replica}",
                    "--state-dir",
                    str(slot_dir),
                    "--env-file",
                    str(env_path),
                ],
                "WorkingDirectory": str(project_root),
                "EnvironmentVariables": {"RUNNER_VERSION": runner_version},
                "RunAtLoad": True,
                "KeepAlive": True,
                "ProcessType": "Background",
                "StandardOutPath": str(log_path),
                "StandardErrorPath": str(log_path),
            }
            rendered.append((label, plist))

    return rendered


def write_rendered(output_dir: Path, rendered: list[tuple[str, dict]]) -> None:
    output_dir.mkdir(parents=True, mode=0o700, exist_ok=True)

    # Only replace files owned by this renderer. Do not recursively remove an
    # arbitrary caller-supplied directory: its path is user input.
    for generated_file in output_dir.glob(f"{LABEL_PREFIX}.*.plist"):
        generated_file.unlink()
    (output_dir / "manifest.txt").unlink(missing_ok=True)

    manifest: list[str] = []
    for label, plist in rendered:
        plist_path = output_dir / f"{label}.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(plist, handle, sort_keys=False)
        manifest.append(label)

    (output_dir / "manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render optional config/repos.yaml macOS runners as launchd agents."
    )
    parser.add_argument(
        "--repos-yaml", type=Path, default=Path("config/repos.yaml"), help="repo config"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".runner-macos/launchd"),
        help="generated launchd plist directory",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="absolute paths in plists are rooted here",
    )
    parser.add_argument(
        "--runner-version",
        default="2.336.0",
        help="pinned actions/runner version passed to the launch agents",
    )
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    repos_yaml = args.repos_yaml.resolve()
    output_dir = args.output_dir.resolve()
    if not repos_yaml.is_file():
        parser.error(f"{repos_yaml} does not exist")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.runner_version):
        parser.error("--runner-version must be in major.minor.patch form")

    try:
        with repos_yaml.open(encoding="utf-8") as handle:
            repos_config = yaml.safe_load(handle)
        rendered = render(repos_config, project_root, output_dir, args.runner_version)
        if not rendered:
            fail("no repos define a macos runner; add a 'macos:' section to config/repos.yaml")
        write_rendered(output_dir, rendered)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))

    print(f"wrote {output_dir} ({len(rendered)} macOS launchd agent(s))")


if __name__ == "__main__":
    main()
