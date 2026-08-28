#!/usr/bin/env python3
"""Render per-runner Windows Scheduled Tasks from the optional ``windows`` repo config."""

from __future__ import annotations

import argparse
import getpass
import os
import re
from pathlib import Path
from xml.sax.saxutils import escape

import yaml

TASK_FOLDER = "GitHubSelfHostedRunner"
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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


def task_name(repo: str, replica: int) -> str:
    name = f"runner-{repo.lower()}-windows-{replica}"
    if not TASK_NAME_RE.fullmatch(name):
        fail(
            f"repo {repo!r} produces unsupported scheduled task name {name!r}; "
            "use a GitHub repository name containing only letters, digits, "
            "underscores, and hyphens"
        )
    return name


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    username = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{username}" if domain else username


def render_task_xml(
    *,
    script_path: Path,
    env_path: Path,
    state_dir: Path,
    owner: str,
    repo: str,
    labels: list[str],
    runner_name: str,
    user: str,
    runner_version: str,
) -> str:
    arguments = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{script_path}" '
        f'-Owner "{owner}" -Repo "{repo}" -Labels "{",".join(labels)}" '
        f'-RunnerName "{runner_name}" -StateDir "{state_dir}" -EnvFile "{env_path}" '
        f'-RunnerVersion "{runner_version}"'
    )
    description = f"github_self-hosted_runner native Windows runner for {owner}/{repo} ({runner_name})"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>pwsh.exe</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(state_dir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def render(
    repos_config: dict,
    project_root: Path,
    runner_version: str,
    user: str,
) -> list[tuple[str, str]]:
    if not isinstance(repos_config, dict) or not isinstance(repos_config.get("repos"), list):
        fail("top-level 'repos' must be a list")

    script_path = project_root / "scripts" / "windows-runner-loop.ps1"
    env_path = project_root / ".env"
    state_root = project_root / ".runner-windows"
    rendered: list[tuple[str, str]] = []

    for repo_index, entry in enumerate(repos_config["repos"], start=1):
        context = f"repos[{repo_index}]"
        if not isinstance(entry, dict):
            fail(f"{context} must be a mapping")

        windows = entry.get("windows")
        if windows is None:
            continue
        if not isinstance(windows, dict):
            fail(f"{context}.windows must be a mapping")

        owner = require_string(entry, "owner", context)
        repo = require_string(entry, "repo", context)
        labels = require_labels(windows.get("labels", ["windows"]), f"{context}.windows")
        replicas = require_replicas(windows.get("replicas", 1), f"{context}.windows")
        runner_name_prefix = windows.get("runner_name_prefix", f"runner-{repo.lower()}-windows")
        if not isinstance(runner_name_prefix, str) or not runner_name_prefix:
            fail(f"{context}.windows.runner_name_prefix must be a non-empty string")

        for replica in range(1, replicas + 1):
            name = task_name(repo, replica)
            slot_dir = state_root / "slots" / name
            xml = render_task_xml(
                script_path=script_path,
                env_path=env_path,
                state_dir=slot_dir,
                owner=owner,
                repo=repo,
                labels=labels,
                runner_name=f"{runner_name_prefix}-{replica}",
                user=user,
                runner_version=runner_version,
            )
            rendered.append((name, xml))

    return rendered


def write_rendered(output_dir: Path, rendered: list[tuple[str, str]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Only replace files owned by this renderer. Do not recursively remove an
    # arbitrary caller-supplied directory: its path is user input.
    for generated_file in output_dir.glob("*.xml"):
        generated_file.unlink()
    (output_dir / "manifest.txt").unlink(missing_ok=True)

    manifest: list[str] = []
    for name, xml in rendered:
        xml_path = output_dir / f"{name}.xml"
        xml_path.write_text(xml, encoding="utf-8")
        manifest.append(name)

    (output_dir / "manifest.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render optional config/repos.yaml Windows runners as Scheduled Tasks."
    )
    parser.add_argument(
        "--repos-yaml", type=Path, default=Path("config/repos.yaml"), help="repo config"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".runner-windows/scheduled-tasks"),
        help="generated Scheduled Task XML directory",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="absolute paths in the task XML are rooted here",
    )
    parser.add_argument(
        "--runner-version",
        default="2.336.0",
        help="pinned actions/runner version passed to the scheduled tasks",
    )
    parser.add_argument(
        "--user",
        default=current_user(),
        help="DOMAIN\\User (or User) the logon trigger and principal run as; "
        "defaults to the current user",
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
        rendered = render(repos_config, project_root, args.runner_version, args.user)
        if not rendered:
            fail("no repos define a windows runner; add a 'windows:' section to config/repos.yaml")
        write_rendered(output_dir, rendered)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))

    print(f"wrote {output_dir} ({len(rendered)} Windows Scheduled Task(s))")


if __name__ == "__main__":
    main()
