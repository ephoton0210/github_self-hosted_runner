#!/usr/bin/env python3
"""Render config/repos.yaml into compose.generated.yaml.

One Compose service per (repo, replica) pair, per
development/02_DEPLOYMENT_DESIGN.md. Adding a repo is a config/repos.yaml
edit + `docker compose up -d`, not a manual runbook.

Usage: scripts/render-compose.py [repos.yaml] [-o compose.generated.yaml]
"""
import argparse
import pathlib
import sys

import yaml

GENERATED_HEADER = (
    "# GENERATED FILE — do not edit by hand.\n"
    "# Regenerate with: scripts/render-compose.py\n"
    "# Source of truth: config/repos.yaml\n\n"
)


def render(repos_config):
    services = {}
    for entry in repos_config["repos"]:
        owner = entry["owner"]
        repo = entry["repo"]
        labels = entry["labels"]
        replicas = entry.get("replicas", 1)

        for i in range(1, replicas + 1):
            name = f"runner-{repo}-{i}"
            services[name] = {
                "build": "./docker/runner",
                "container_name": name,
                "restart": "unless-stopped",
                "env_file": [".env"],
                "environment": {
                    "GH_OWNER": owner,
                    "GH_REPO": repo,
                    "RUNNER_NAME": name,
                    "RUNNER_LABELS": ",".join(labels),
                },
                "volumes": ["/var/run/docker.sock:/var/run/docker.sock"],
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

    repos_config = yaml.safe_load(args.repos_yaml.read_text())
    compose = render(repos_config)

    args.output.write_text(
        GENERATED_HEADER + yaml.dump(compose, sort_keys=False, default_flow_style=False)
    )
    print(f"wrote {args.output} ({len(compose['services'])} service(s))")


if __name__ == "__main__":
    main()
