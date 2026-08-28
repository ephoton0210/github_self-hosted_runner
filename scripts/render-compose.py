#!/usr/bin/env python3
"""Render config/repos.yaml into compose.generated.yaml.

One Compose service per (repo, replica) pair, per
development/02_DEPLOYMENT_DESIGN.md. Adding a repo is a config/repos.yaml
edit + `docker compose up -d`, not a manual runbook.

Usage: scripts/render-compose.py [repos.yaml] [-o compose.generated.yaml]
"""
import argparse
import pathlib
import platform
import sys

import yaml

GENERATED_HEADER = (
    "# GENERATED FILE — do not edit by hand.\n"
    "# Regenerate with: scripts/render-compose.py\n"
    "# Source of truth: config/repos.yaml\n\n"
)


def default_name_prefix(repo: str, *, host_is_windows: bool) -> str:
    # These are still Linux containers on a Windows host (Docker Desktop), not
    # native Windows runners — the "-windows-docker" suffix keeps them visibly
    # distinct, in both `docker compose ps` and the GitHub runners list, from
    # both a Linux-host fleet for the same repo and a native `windows:` fleet
    # (which defaults to a "-windows" suffix; see render-windows-scheduled-task.py).
    suffix = "-windows-docker" if host_is_windows else ""
    return f"runner-{repo.lower()}{suffix}"


def render(repos_config, *, host_is_windows: bool = platform.system() == "Windows"):
    services = {}
    for entry in repos_config["repos"]:
        owner = entry["owner"]
        repo = entry["repo"]
        labels = entry["labels"]
        replicas = entry.get("replicas", 1)
        default_prefix = default_name_prefix(repo, host_is_windows=host_is_windows)
        runner_name_prefix = entry.get("runner_name_prefix", default_prefix)

        for i in range(1, replicas + 1):
            service_name = f"{default_prefix}-{i}"
            runner_name = f"{runner_name_prefix}-{i}"
            services[service_name] = {
                "build": {
                    "context": ".",
                    "dockerfile": "docker/runner/Dockerfile",
                },
                "image": "github-actions-runner:latest",
                "container_name": service_name,
                "restart": "unless-stopped",
                "stop_grace_period": "30s",
                "env_file": [".env"],
                "environment": {
                    "GH_OWNER": owner,
                    "GH_REPO": repo,
                    "RUNNER_NAME": runner_name,
                    "RUNNER_LABELS": ",".join(labels),
                },
                "volumes": [
                    "/var/run/docker.sock:/var/run/docker.sock",
                    # Host-shared docker/build-push-action `type=local` buildx
                    # cache dir (see modelforge-build-check.yml's cache-from/
                    # cache-to). Must be a real bind mount, not left implicit:
                    # the buildx CLI performing local cache I/O runs inside
                    # *this* runner container, not on the host, so without
                    # this mount `/opt/buildx-cache` is just each replica's
                    # own ephemeral container-local writable layer -- neither
                    # shared between runner-<repo>-1/-2 nor durable across a
                    # container recreate.
                    "/opt/buildx-cache:/opt/buildx-cache",
                ],
            }

    return {"services": services}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repos_yaml",
        nargs="?",
        default="config/repos.yaml",
        type=pathlib.Path,
    )
    parser.add_argument(
        "-o",
        "--output",
        default="compose.generated.yaml",
        type=pathlib.Path,
    )
    args = parser.parse_args()

    if not args.repos_yaml.exists():
        example = args.repos_yaml.with_suffix(".yaml.example")
        sys.exit(
            f"{args.repos_yaml} not found. Copy {example} to "
            f"{args.repos_yaml} and fill in real values."
        )

    repos_config = yaml.safe_load(args.repos_yaml.read_text(encoding="utf-8"))
    compose = render(repos_config)

    args.output.write_text(
        GENERATED_HEADER + yaml.dump(compose, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"wrote {args.output} ({len(compose['services'])} service(s))")


if __name__ == "__main__":
    main()
