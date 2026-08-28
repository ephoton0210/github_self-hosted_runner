#!/usr/bin/env python3
"""Local status dashboard for this host's self-hosted runner fleet.

Reads only local state — Docker (Linux container fleet), macOS launchd, and
Windows Scheduled Tasks — never the GitHub API. It shows what start.sh /
start-macos.sh / start-windows.ps1 actually started on *this* host: which
runners exist, whether each is idle, running a job, starting up, or stopped,
and a tail of each runner's own log. A repo's fleet may span multiple hosts;
by default each host's dashboard only sees its own — use --peer (static) or
--register-to/--advertise-url (a satellite self-registers with a central
dashboard) to merge others in. Either way this stays read-only in both
directions: a merged host is only ever fetched from, never sent a command.

Usage: scripts/dashboard.py [--host 127.0.0.1] [--port 8787]


FUNCTION OVERVIEW (file order — search for a name to jump to it)

  Helpers
    infer_state(lines)                 -> (state, detail) from a runner's own log markers
    tail_lines(path, count)            -> last `count` lines of a file, or [] if missing
    run(cmd)                           -> stdout, or None on any failure (never raises)

  Host-level resource usage
    collect_host_resources()           -> this host's CPU load / memory (stdlib+CLI, no psutil)
    collect_docker_stats()             -> per-container CPU%/mem, from `docker stats`

  Per-fleet-type collectors — each returns {"available", "reason"?, "runners": [...]}
    collect_docker_runners()           -> Linux container fleet (Docker / Docker Desktop)
    collect_macos_runners()            -> native macOS fleet (launchd)
    parse_windows_task_args(args)      -> Owner/Repo/RunnerName out of a Scheduled Task's <Arguments>
    collect_windows_runners()          -> native Windows fleet (Scheduled Tasks)

  This host's own status (GET /api/status)
    build_status()                     -> {host, os, resources, sections} — local only
    fetch_log(id, tail)                -> local log tail for one runner ("kind:name" id)

  Multi-host fleet aggregation (GET /api/fleet, GET /api/logs, POST /api/register)
    qualify_runner_ids(sections, label) -> rewrites ids to "label::kind:name" in place
    all_peers()                        -> live peers: static --peer + fresh self-registrations
    register_peer(label, url)          -> validates and stores a POST /api/register
    fetch_peer_status(label, url)      -> one host entry for build_fleet() (never raises)
    build_fleet()                      -> self + all_peers(), each host-qualified
    fetch_log_routed(id, tail)         -> routes a (possibly host-qualified) id, local or proxied

  INDEX_HTML — the served page: inline CSS, then JS that polls /api/fleet (patches rows
  in place, only rebuilds on a structural change) and /api/logs (appends new lines only)

  HTTP server
    DashboardHandler.do_GET/do_POST    -> routes /, /api/status, /api/fleet, /api/logs, /api/register
    registration_loop(targets, ...)    -> background --register-to heartbeat thread
    main()                             -> CLI entrypoint; see --help for every flag
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

import yaml

# Set from --label / --peer in main() before the server starts; read-only
# thereafter, so concurrent request handlers can read them without locking.
SELF_LABEL = platform.node()
PEERS: dict[str, str] = {}  # static, from --peer — never expires

# Dynamic, from POST /api/register (see "Self-registration" below) — mutated
# from request-handler threads, so every read/write goes through _peers_lock.
DYNAMIC_PEERS: dict[str, dict] = {}  # label -> {"url": str, "last_seen": monotonic float}
DYNAMIC_PEER_TTL = 90  # seconds; ~4-5 missed heartbeats at the default 20s interval
_peers_lock = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "compose.generated.yaml"
MACOS_LAUNCHD_DIR = PROJECT_ROOT / ".runner-macos" / "launchd"
MACOS_LOG_DIR = PROJECT_ROOT / ".runner-macos" / "logs"
WINDOWS_TASK_DIR = PROJECT_ROOT / ".runner-windows" / "scheduled-tasks"
WINDOWS_LOG_DIR = PROJECT_ROOT / ".runner-windows" / "logs"
WINDOWS_TASK_FOLDER = "\\GitHubSelfHostedRunner\\"

RUNNING_RE = re.compile(r"Running job:\s*(.+)")
LISTENING_RE = re.compile(r"Listening for Jobs")
READY_RE = re.compile(r"is ready for one job")
TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def infer_state(lines: list[str]) -> tuple[str, str]:
    """Scan log lines (oldest first) backward for the runner's own status
    markers — the same three lines README.md already documents as ground
    truth for "is this runner idle or on a job."
    """
    for line in reversed(lines):
        match = RUNNING_RE.search(line)
        if match:
            return "running", match.group(1).strip()
        if LISTENING_RE.search(line):
            return "idle", "Listening for Jobs"
        if READY_RE.search(line):
            return "starting", "ready for one job"
    return "unknown", ""


def tail_lines(path: Path, count: int) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-count:]]


def run(cmd: list[str], cwd: Path | None = None, timeout: float = 10) -> str | None:
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout


# --- Host-level resource usage ----------------------------------------------


def collect_host_resources() -> dict:
    """Best-effort, stdlib/CLI-only CPU and memory snapshot for this host.
    Answers "is this host under contention" for the runners on it — not a
    per-process breakdown (see collect_docker_stats for the one fleet type
    that gives us that for free, via `docker stats`).
    """
    system = platform.system()
    cpu_count = os.cpu_count()
    load1 = None
    cpu_percent = None
    mem_used_mb = None
    mem_total_mb = None

    if hasattr(os, "getloadavg"):
        try:
            load1 = os.getloadavg()[0]
            if cpu_count:
                cpu_percent = min(100.0, (load1 / cpu_count) * 100)
        except OSError:
            pass

    if system == "Linux":
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(":")
                info[key] = value.strip()
            total_kb = int(info.get("MemTotal", "0 kB").split()[0])
            available_kb = int(info.get("MemAvailable", "0 kB").split()[0])
            mem_total_mb = total_kb // 1024
            mem_used_mb = (total_kb - available_kb) // 1024
        except (OSError, ValueError, IndexError):
            pass
    elif system == "Darwin":
        total_output = run(["sysctl", "-n", "hw.memsize"])
        vm_output = run(["vm_stat"])
        if total_output:
            try:
                mem_total_mb = int(total_output.strip()) // (1024 * 1024)
            except ValueError:
                pass
        if vm_output and mem_total_mb:
            try:
                page_size = 4096
                page_match = re.search(r"page size of (\d+) bytes", vm_output)
                if page_match:
                    page_size = int(page_match.group(1))
                pages = {}
                for line in vm_output.splitlines():
                    match = re.match(r"^(Pages [a-z]+):\s+(\d+)\.?", line)
                    if match:
                        pages[match.group(1)] = int(match.group(2))
                # "available" here follows Activity Monitor's definition (free +
                # inactive, both reclaimable without swapping), not just free pages.
                available_bytes = (pages.get("Pages free", 0) + pages.get("Pages inactive", 0)) * page_size
                mem_used_mb = (mem_total_mb * 1024 * 1024 - available_bytes) // (1024 * 1024)
            except (ValueError, TypeError):
                pass
    elif system == "Windows":
        mem_output = run(["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize", "/value"])
        if mem_output:
            values = dict(line.strip().split("=", 1) for line in mem_output.splitlines() if "=" in line)
            try:
                total_kb = int(values.get("TotalVisibleMemorySize", "0"))
                free_kb = int(values.get("FreePhysicalMemory", "0"))
                mem_total_mb = total_kb // 1024
                mem_used_mb = (total_kb - free_kb) // 1024
            except ValueError:
                pass
        load_output = run(["wmic", "cpu", "get", "loadpercentage", "/value"])
        if load_output:
            match = re.search(r"LoadPercentage=(\d+)", load_output)
            if match:
                cpu_percent = float(match.group(1))

    return {
        "cpu_count": cpu_count,
        "load1": round(load1, 2) if load1 is not None else None,
        "cpu_percent": round(cpu_percent, 1) if cpu_percent is not None else None,
        "mem_used_mb": mem_used_mb,
        "mem_total_mb": mem_total_mb,
    }


def collect_docker_stats() -> dict[str, dict]:
    """Live per-container CPU/mem from `docker stats` — real usage, not an
    estimate, and it costs nothing extra: Docker already tracks it.
    """
    output = run(["docker", "stats", "--no-stream", "--format", "json"], timeout=15)
    if not output:
        return {}
    text = output.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    stats = {}
    for entry in entries:
        name = entry.get("Name")
        if not name:
            continue
        cpu_percent = None
        try:
            cpu_percent = float(entry.get("CPUPerc", "").rstrip("%"))
        except ValueError:
            pass
        stats[name] = {"cpu_percent": cpu_percent, "mem_usage": entry.get("MemUsage", "")}
    return stats


# --- Linux container fleet (Docker / Docker Desktop) -----------------------


def collect_docker_runners() -> dict:
    if not COMPOSE_FILE.is_file():
        return {"available": False, "reason": "no compose.generated.yaml (run start.sh / start.ps1)", "runners": []}

    try:
        compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
        services = compose.get("services", {}) if compose else {}
    except (OSError, yaml.YAMLError) as error:
        return {"available": False, "reason": f"could not parse compose.generated.yaml: {error}", "runners": []}

    if not services:
        return {"available": False, "reason": "compose.generated.yaml has no services", "runners": []}

    live_output = run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "ps", "--all", "--format", "json"],
        cwd=PROJECT_ROOT,
    )
    if live_output is None:
        return {"available": False, "reason": "docker CLI not available or daemon not running", "runners": []}

    live = {}
    text = live_output.strip()
    if text:
        try:
            parsed = json.loads(text)
            entries = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            entries = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for entry in entries:
            name = entry.get("Name") or entry.get("Service")
            if name:
                live[name] = entry

    any_running = any(c.get("State") == "running" for c in live.values())
    docker_stats = collect_docker_stats() if any_running else {}

    runners = []
    for service_name, service in services.items():
        env = service.get("environment", {})
        owner = env.get("GH_OWNER", "?")
        repo = env.get("GH_REPO", "?")
        runner_name = env.get("RUNNER_NAME", service_name)
        labels = env.get("RUNNER_LABELS", "")
        container = live.get(service_name)
        cpu_percent, mem_usage = None, None

        if container is None or container.get("State") != "running":
            state, detail = "stopped", (container.get("Status") if container else "not created")
        else:
            logs = run(["docker", "logs", "--tail", "60", service_name], cwd=PROJECT_ROOT) or ""
            state, detail = infer_state(logs.splitlines())
            if state == "unknown":
                detail = container.get("Status", "")
            stat = docker_stats.get(service_name)
            if stat:
                cpu_percent, mem_usage = stat["cpu_percent"], stat["mem_usage"]

        runners.append(
            {
                "id": f"docker:{service_name}",
                "kind": "linux-docker",
                "name": runner_name,
                "repo": f"{owner}/{repo}",
                "labels": labels,
                "state": state,
                "detail": detail,
                "cpu_percent": cpu_percent,
                "mem_usage": mem_usage,
            }
        )

    return {"available": True, "runners": runners}


# --- Native macOS fleet ------------------------------------------------------


def collect_macos_runners() -> dict:
    manifest = MACOS_LAUNCHD_DIR / "manifest.txt"
    if not manifest.is_file():
        return {"available": False, "reason": "no native macOS fleet on this host (run start-macos.sh)", "runners": []}

    labels = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    uid = None
    if hasattr(os, "getuid"):
        uid = os.getuid()

    runners = []
    for label in labels:
        plist_path = MACOS_LAUNCHD_DIR / f"{label}.plist"
        owner, repo, runner_name = "?", "?", label
        if plist_path.is_file():
            try:
                with plist_path.open("rb") as handle:
                    plist = plistlib.load(handle)
                args = plist.get("ProgramArguments", [])
                if "--owner" in args:
                    owner = args[args.index("--owner") + 1]
                if "--repo" in args:
                    repo = args[args.index("--repo") + 1]
                if "--runner-name" in args:
                    runner_name = args[args.index("--runner-name") + 1]
            except (OSError, ValueError, IndexError, plistlib.InvalidFileException):
                pass

        loaded = True
        if uid is not None:
            output = run(["launchctl", "print", f"gui/{uid}/{label}"])
            loaded = output is not None

        log_lines = tail_lines(MACOS_LOG_DIR / f"{runner_name}.log", 60)
        if not loaded:
            state, detail = "stopped", "not loaded"
        else:
            state, detail = infer_state(log_lines)

        runners.append(
            {
                "id": f"macos:{runner_name}",
                "kind": "macos",
                "name": runner_name,
                "repo": f"{owner}/{repo}",
                "labels": "",
                "state": state,
                "detail": detail,
                # Per-process CPU/mem isn't tracked yet for native runners (would
                # need to resolve launchd's agent PID down to the actual
                # Runner.Listener child) — collect_host_resources() still
                # covers "is this host under contention" for this fleet type.
                "cpu_percent": None,
                "mem_usage": None,
            }
        )

    return {"available": True, "runners": runners}


# --- Native Windows fleet ----------------------------------------------------


def parse_windows_task_args(arguments: str) -> dict:
    values = {}
    for flag in ("Owner", "Repo", "RunnerName"):
        match = re.search(rf'-{flag}\s+"([^"]*)"', arguments)
        if match:
            values[flag] = match.group(1)
    return values


def collect_windows_runners() -> dict:
    manifest = WINDOWS_TASK_DIR / "manifest.txt"
    if not manifest.is_file():
        return {"available": False, "reason": "no native Windows fleet on this host (run start-windows.ps1)", "runners": []}

    names = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

    runners = []
    for name in names:
        xml_path = WINDOWS_TASK_DIR / f"{name}.xml"
        owner, repo, runner_name = "?", "?", name
        if xml_path.is_file():
            try:
                root = ET.fromstring(xml_path.read_text(encoding="utf-8"))
                exec_el = root.find("./t:Actions/t:Exec", TASK_NS)
                args_el = exec_el.find("t:Arguments", TASK_NS) if exec_el is not None else None
                if args_el is not None and args_el.text:
                    parsed = parse_windows_task_args(args_el.text)
                    owner = parsed.get("Owner", owner)
                    repo = parsed.get("Repo", repo)
                    runner_name = parsed.get("RunnerName", runner_name)
            except ET.ParseError:
                pass

        task_output = run(["schtasks", "/Query", "/TN", f"{WINDOWS_TASK_FOLDER}{name}", "/FO", "LIST"])
        if task_output is None:
            state, detail = "unknown", "schtasks unavailable"
        else:
            status_match = re.search(r"^Status:\s*(.+)$", task_output, re.MULTILINE)
            task_status = status_match.group(1).strip() if status_match else ""
            if task_status.lower() != "running" and "not found" in task_output.lower():
                state, detail = "stopped", "task not registered"
            else:
                log_lines = tail_lines(WINDOWS_LOG_DIR / f"{runner_name}.log", 60)
                state, detail = infer_state(log_lines)
                if state == "unknown":
                    detail = task_status or "starting"

        runners.append(
            {
                "id": f"windows:{runner_name}",
                "kind": "windows",
                "name": runner_name,
                "repo": f"{owner}/{repo}",
                "labels": "",
                "state": state,
                "detail": detail,
                # See the matching comment in collect_macos_runners().
                "cpu_percent": None,
                "mem_usage": None,
            }
        )

    return {"available": True, "runners": runners}


def build_status() -> dict:
    return {
        "host": platform.node(),
        "os": platform.system(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resources": collect_host_resources(),
        "sections": {
            "linux-docker": collect_docker_runners(),
            "macos": collect_macos_runners(),
            "windows": collect_windows_runners(),
        },
    }


def fetch_log(runner_id: str, tail: int) -> list[str]:
    try:
        kind, name = runner_id.split(":", 1)
    except ValueError:
        return ["invalid runner id"]

    if kind == "docker":
        output = run(["docker", "logs", "--tail", str(tail), name], cwd=PROJECT_ROOT)
        if output is None:
            return ["docker CLI not available or container not found"]
        return output.splitlines()
    if kind == "macos":
        return tail_lines(MACOS_LOG_DIR / f"{name}.log", tail) or ["(no log yet)"]
    if kind == "windows":
        return tail_lines(WINDOWS_LOG_DIR / f"{name}.log", tail) or ["(no log yet)"]
    return [f"unknown runner kind: {kind}"]


# --- Multi-host fleet aggregation --------------------------------------------
#
# Each dashboard instance is still the sole source of truth for its own host
# (everything above this point is unchanged local-only logic, and /api/status
# keeps returning exactly that). A peer just means: also fetch that plain
# /api/status from other hosts' dashboard instances and merge the results —
# no new central service, no new protocol, the peer is a normal client of the
# exact same endpoint a browser would hit. This is read-only in both
# directions: nothing here ever sends a peer a command, only requests its
# already-local-only status.
#
# Two ways a peer gets into that list: statically via --peer LABEL=URL
# (never expires, the operator typed it), or dynamically via a satellite
# host's own --register-to POSTing itself to this instance's /api/register
# (expires DYNAMIC_PEER_TTL seconds after its last heartbeat, so a host that
# goes away — not just unreachable right now, actually decommissioned or
# reconfigured — eventually drops out instead of showing up forever).


def qualify_runner_ids(sections: dict, label: str) -> None:
    for section in sections.values():
        for runner in section.get("runners", []):
            runner["id"] = f"{label}::{runner['id']}"


def all_peers() -> dict[str, str]:
    now = time.monotonic()
    with _peers_lock:
        expired = [label for label, info in DYNAMIC_PEERS.items() if now - info["last_seen"] > DYNAMIC_PEER_TTL]
        for label in expired:
            del DYNAMIC_PEERS[label]
        dynamic = {label: info["url"] for label, info in DYNAMIC_PEERS.items()}
    # A statically-configured --peer always wins a label collision — it's an
    # explicit operator choice, a self-registration is just a convenience.
    return {**dynamic, **PEERS}


def register_peer(label: object, url: object) -> tuple[bool, str]:
    label = str(label).strip() if label else ""
    url = str(url).strip().rstrip("/") if url else ""
    if not label or not url:
        return False, "label and url are required"
    if not (url.startswith("http://") or url.startswith("https://")):
        return False, "url must start with http:// or https://"
    if label == SELF_LABEL:
        return False, f"label {label!r} collides with this host's own --label"
    if label in PEERS:
        return False, f"label {label!r} is already a statically-configured --peer"
    with _peers_lock:
        DYNAMIC_PEERS[label] = {"url": url, "last_seen": time.monotonic()}
    return True, "registered"


def fetch_peer_status(label: str, url: str) -> dict:
    try:
        request = Request(f"{url}/api/status", headers={"User-Agent": "runner-dashboard-peer"})
        with urlopen(request, timeout=5) as response:  # noqa: S310 - operator-supplied peer URL, trusted network
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, ValueError) as error:
        return {"label": label, "reachable": False, "error": str(error), "sections": {}, "resources": {}}

    sections = data.get("sections", {})
    qualify_runner_ids(sections, label)
    return {
        "label": label,
        "reachable": True,
        "host": data.get("host", label),
        "os": data.get("os", "?"),
        "generated_at": data.get("generated_at"),
        "resources": data.get("resources", {}),
        "sections": sections,
    }


def build_fleet() -> dict:
    local = build_status()
    qualify_runner_ids(local["sections"], SELF_LABEL)
    hosts = [
        {
            "label": SELF_LABEL,
            "reachable": True,
            "host": local["host"],
            "os": local["os"],
            "generated_at": local["generated_at"],
            "resources": local["resources"],
            "sections": local["sections"],
        }
    ]
    for label, url in all_peers().items():
        hosts.append(fetch_peer_status(label, url))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "hosts": hosts}


def fetch_log_routed(runner_id: str, tail: int) -> dict:
    if "::" in runner_id:
        label, _, remainder = runner_id.partition("::")
    else:
        label, remainder = SELF_LABEL, runner_id

    if label == SELF_LABEL:
        return {"id": runner_id, "lines": fetch_log(remainder, tail)}

    url = all_peers().get(label)
    if not url:
        return {"id": runner_id, "lines": [f"unknown host label: {label}"]}
    try:
        query = urlencode({"id": remainder, "tail": tail})
        request = Request(f"{url}/api/logs?{query}", headers={"User-Agent": "runner-dashboard-peer"})
        with urlopen(request, timeout=8) as response:  # noqa: S310 - operator-supplied peer URL, trusted network
            data = json.loads(response.read().decode("utf-8"))
        return {"id": runner_id, "lines": data.get("lines", [])}
    except (URLError, OSError, ValueError) as error:
        return {"id": runner_id, "lines": [f"could not reach host {label!r}: {error}"]}


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Self-Hosted Runner Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0f1115; --panel: #171a21; --border: #2a2f3a; --text: #e6e8eb;
    --muted: #8b93a1; --idle: #3fb950; --running: #58a6ff; --starting: #d29922;
    --stopped: #6e7681; --unknown: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  header {
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 8px;
  }
  header h1 { font-size: 18px; margin: 0; }
  header .meta { color: var(--muted); font-size: 12px; }
  main { padding: 16px 24px; max-width: 1200px; margin: 0 auto; }
  .host-block { margin-bottom: 32px; padding-bottom: 8px; }
  .host-block + .host-block { border-top: 1px solid var(--border); padding-top: 20px; }
  .host-header {
    display: flex; justify-content: space-between; align-items: baseline;
    flex-wrap: wrap; gap: 8px; margin-bottom: 12px;
  }
  .host-title { font-size: 16px; margin: 0; }
  .host-title .host-os { color: var(--muted); font-weight: 400; font-size: 13px; }
  .host-resources { color: var(--muted); font-size: 12px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  section { margin-bottom: 24px; }
  section h2 { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin: 0 0 8px; }
  .empty { color: var(--muted); padding: 12px; border: 1px dashed var(--border); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td.num { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; white-space: nowrap; }
  tr.runner-row { cursor: pointer; }
  tr.runner-row:hover { background: #1c2129; }
  .badge { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; }
  .badge.idle .dot { background: var(--idle); } .badge.idle { color: var(--idle); }
  .badge.running .dot { background: var(--running); box-shadow: 0 0 0 0 var(--running); animation: pulse 1.6s infinite; }
  .badge.running { color: var(--running); }
  .badge.starting .dot { background: var(--starting); } .badge.starting { color: var(--starting); }
  .badge.stopped .dot { background: var(--stopped); } .badge.stopped { color: var(--stopped); }
  .badge.unknown .dot { background: var(--unknown); } .badge.unknown { color: var(--unknown); }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(88,166,255,.5); } 100% { box-shadow: 0 0 0 6px rgba(88,166,255,0); } }
  .detail { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .log-panel { display: none; background: #0b0d11; border: 1px solid var(--border); border-radius: 8px; margin: 4px 0 12px; }
  .log-panel.open { display: block; }
  .log-panel pre {
    margin: 0; padding: 12px; white-space: pre-wrap; word-break: break-word;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; max-height: 360px; overflow-y: auto;
  }
  .log-panel .log-head { display: flex; justify-content: space-between; padding: 6px 12px; color: var(--muted); font-size: 11px; border-bottom: 1px solid var(--border); }
</style>
</head>
<body>
<header>
  <h1>Self-Hosted Runner Dashboard</h1>
  <div class="meta" id="meta">loading...</div>
</header>
<main id="main">
  <div class="empty">Loading runner status...</div>
</main>
<script>
const SECTION_TITLES = {"linux-docker": "Linux Containers (Docker)", "macos": "Native macOS", "windows": "Native Windows"};
const STATE_LABELS = {idle: "Idle", running: "Running", starting: "Starting", stopped: "Stopped", unknown: "Unknown"};
const RUNNER_COLS = 6; // Name, Repo, State, CPU, Mem, Detail
let openLogId = null;
let logTimer = null;
let logState = {}; // runner id -> last-fetched lines, for incremental append
let lastFleetShape = null; // see fleetShape() — patch in place vs full rebuild

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function badge(state, label) {
  const cls = STATE_LABELS[state] ? state : "unknown";
  return `<span class="badge ${cls}"><span class="dot"></span>${label || STATE_LABELS[cls]}</span>`;
}

function formatResources(res) {
  if (!res) return "resource data unavailable";
  const parts = [];
  if (res.cpu_count) parts.push(`${res.cpu_count} cores`);
  if (res.load1 != null) parts.push(`load ${res.load1}`);
  if (res.cpu_percent != null) parts.push(`${res.cpu_percent}% CPU`);
  if (res.mem_used_mb != null && res.mem_total_mb != null) {
    const used = (res.mem_used_mb / 1024).toFixed(1);
    const total = (res.mem_total_mb / 1024).toFixed(1);
    parts.push(`${used}/${total} GB mem`);
  }
  return parts.length ? parts.join(" · ") : "resource data unavailable";
}

function renderRunnerRow(r) {
  const id = escapeHtml(r.id);
  const cpu = r.cpu_percent != null ? `${r.cpu_percent}%` : "—";
  const mem = r.mem_usage ? escapeHtml(r.mem_usage) : "—";
  return `<tr class="runner-row" data-id="${id}">
    <td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.repo)}</td><td>${badge(r.state)}</td>
    <td class="num">${cpu}</td><td class="num">${mem}</td>
    <td class="detail">${escapeHtml(r.detail || "")}</td>
  </tr>
  <tr class="log-row" data-log-for="${id}"><td colspan="${RUNNER_COLS}"><div class="log-panel" id="log-${cssId(r.id)}">
    <div class="log-head"><span>tail -f (this runner's own log)</span><span class="log-updated"></span></div>
    <pre class="log-body">loading...</pre>
  </div></td></tr>`;
}

function cssId(id) { return id.replace(/[^a-zA-Z0-9_-]/g, "_"); }

function renderSection(key, data) {
  const title = SECTION_TITLES[key];
  if (!data.available) {
    return `<section><h2>${title}</h2><div class="empty">${escapeHtml(data.reason)}</div></section>`;
  }
  if (data.runners.length === 0) {
    return `<section><h2>${title}</h2><div class="empty">No runners rendered yet for this fleet.</div></section>`;
  }
  const rows = data.runners.map(renderRunnerRow).join("");
  return `<section><h2>${title}</h2><table>
    <thead><tr><th>Name</th><th>Repo</th><th>State</th><th>CPU</th><th>Mem</th><th>Detail</th></tr></thead>
    <tbody>${rows}</tbody></table></section>`;
}

function renderHost(h) {
  const title = `<h2 class="host-title">${escapeHtml(h.label)}${h.reachable ? ` <span class="host-os">(${escapeHtml(h.os || "?")})</span>` : ""}</h2>`;
  if (!h.reachable) {
    return `<div class="host-block unreachable" data-host="${escapeHtml(h.label)}">
      <div class="host-header">${title}${badge("unknown", "Unreachable")}</div>
      <div class="empty">${escapeHtml(h.error || "could not reach this host")}</div>
    </div>`;
  }
  const sections = Object.entries(h.sections || {}).map(([k, v]) => renderSection(k, v)).join("");
  return `<div class="host-block" data-host="${escapeHtml(h.label)}">
    <div class="host-header">${title}<div class="host-resources">${escapeHtml(formatResources(h.resources))}</div></div>
    ${sections}
  </div>`;
}

// A fingerprint of "which host/runner rows exist, in what order" — cheap to
// compute and compare. Unchanged between two polls means every row already
// on the page still means the same thing, so we can patch text/badges in
// place instead of tearing down and rebuilding the DOM (which would also
// blow away any open log panel's already-fetched content and scroll
// position — the actual source of the "screen jumps every few seconds"
// jitter this replaces).
function fleetShape(data) {
  const parts = [];
  for (const h of data.hosts) {
    parts.push(`H:${h.label}:${h.reachable}`);
    if (!h.reachable) continue;
    for (const section of Object.values(h.sections || {})) {
      for (const r of section.runners || []) parts.push(`R:${r.id}`);
    }
  }
  return parts.join("|");
}

function patchStatus(data) {
  for (const h of data.hosts) {
    if (!h.reachable) continue;
    const resEl = document.querySelector(`.host-block[data-host="${h.label}"] .host-resources`);
    if (resEl) resEl.textContent = formatResources(h.resources);
    for (const section of Object.values(h.sections || {})) {
      for (const r of section.runners || []) patchRunnerRow(r);
    }
  }
}

function patchRunnerRow(r) {
  const row = document.querySelector(`tr.runner-row[data-id="${r.id}"]`);
  if (!row) return;
  const cells = row.querySelectorAll("td");
  cells[2].innerHTML = badge(r.state); // Name, Repo unchanged; State, CPU, Mem, Detail can.
  cells[3].textContent = r.cpu_percent != null ? `${r.cpu_percent}%` : "—";
  cells[4].textContent = r.mem_usage ? r.mem_usage : "—";
  cells[5].textContent = r.detail || "";
}

async function refreshStatus() {
  const res = await fetch("/api/fleet");
  const data = await res.json();
  const reachable = data.hosts.filter(h => h.reachable).length;
  const suffix = data.hosts.length > 1 ? ` — ${reachable}/${data.hosts.length} host(s) reachable` : "";
  document.getElementById("meta").textContent = `updated ${new Date(data.generated_at).toLocaleTimeString()}${suffix}`;

  const shape = fleetShape(data);
  if (shape === lastFleetShape) {
    patchStatus(data);
    return;
  }
  lastFleetShape = shape;
  logState = {}; // any open log panel's DOM is about to be rebuilt from scratch below
  const main = document.getElementById("main");
  main.innerHTML = data.hosts.map(renderHost).join("");
  main.querySelectorAll(".runner-row").forEach(row => {
    row.addEventListener("click", () => toggleLog(row.dataset.id));
  });
  if (openLogId) {
    const panel = document.getElementById(`log-${cssId(openLogId)}`);
    if (panel) panel.classList.add("open");
  }
}

function toggleLog(id) {
  const panel = document.getElementById(`log-${cssId(id)}`);
  if (!panel) return;
  if (openLogId === id) {
    panel.classList.remove("open");
    delete logState[openLogId];
    openLogId = null;
    clearInterval(logTimer);
    return;
  }
  if (openLogId) {
    const prevPanel = document.getElementById(`log-${cssId(openLogId)}`);
    if (prevPanel) prevPanel.classList.remove("open");
    delete logState[openLogId];
  }
  openLogId = id;
  panel.querySelector(".log-body").textContent = "loading...";
  panel.classList.add("open");
  clearInterval(logTimer);
  refreshLog();
  logTimer = setInterval(refreshLog, 2000);
}

// Runs independently of refreshStatus's timer — a status poll never touches
// an open log panel's DOM (see patchStatus above), so this is the only thing
// that updates it. Appends only the lines that are actually new instead of
// replacing the whole block of text every tick, which is what let people
// select/copy log text and kept the scroll position stable while reading.
async function refreshLog() {
  if (!openLogId) return;
  const res = await fetch(`/api/logs?id=${encodeURIComponent(openLogId)}&tail=200`);
  const data = await res.json();
  const panel = document.getElementById(`log-${cssId(openLogId)}`);
  if (!panel) return;
  const body = panel.querySelector(".log-body");
  const wasAtBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 20;

  const prev = logState[openLogId] || [];
  const next = data.lines;
  const isGrowingTail = prev.length > 0 && next.length >= prev.length
    && next.slice(0, prev.length).join("\\n") === prev.join("\\n");

  if (isGrowingTail) {
    const added = next.slice(prev.length);
    if (added.length > 0) {
      body.appendChild(document.createTextNode((body.textContent ? "\\n" : "") + added.join("\\n")));
    }
  } else {
    // First fetch for this panel, or older lines rolled out of the tail
    // window (log grew past `tail=200` since the last poll) — no safe
    // incremental diff, so fall back to a full (infrequent) replace.
    body.textContent = next.join("\\n");
  }
  logState[openLogId] = next;

  panel.querySelector(".log-updated").textContent = new Date().toLocaleTimeString();
  if (wasAtBottom) body.scrollTop = body.scrollHeight;
}

refreshStatus();
setInterval(refreshStatus, 3000);
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - matches base class signature
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/api/status":
            self._send_json(build_status())
        elif parsed.path == "/api/fleet":
            self._send_json(build_fleet())
        elif parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            runner_id = (query.get("id") or [""])[0]
            try:
                tail = max(1, min(2000, int((query.get("tail") or ["200"])[0])))
            except ValueError:
                tail = 200
            self._send_json(fetch_log_routed(runner_id, tail))
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        if parsed.path != "/api/register":
            self._send_json({"error": "not found"}, status=404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "invalid JSON body"}, status=400)
            return

        ok, message = register_peer(body.get("label"), body.get("url"))
        self._send_json({"ok": ok, "error": None if ok else message}, status=200 if ok else 409)


def registration_loop(targets: list[str], advertise_url: str, interval: float) -> None:
    """Background heartbeat: POST this host's own label+URL to each central
    dashboard in `targets` every `interval` seconds, so --register-to is a
    "set it and forget it" alternative to the central dashboard's operator
    hand-maintaining --peer for every satellite host. One missed heartbeat
    just means one retry next tick — DYNAMIC_PEER_TTL on the receiving end is
    what actually notices and expires a host that's gone for good.
    """
    payload = json.dumps({"label": SELF_LABEL, "url": advertise_url}).encode("utf-8")
    while True:
        for target in targets:
            try:
                request = Request(
                    f"{target}/api/register",
                    data=payload,
                    method="POST",
                    headers={"Content-Type": "application/json", "User-Agent": "runner-dashboard-register"},
                )
                with urlopen(request, timeout=5) as response:  # noqa: S310 - operator-supplied URL, trusted network
                    response.read()
            except (URLError, OSError, ValueError) as error:
                print(f"Warning: could not register with {target}: {error}", file=sys.stderr)
        time.sleep(interval)


def main() -> None:
    global SELF_LABEL, PEERS

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1, loopback-only)")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--label",
        default=None,
        help="this host's name in a multi-host fleet view (default: hostname)",
    )
    parser.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="LABEL=URL",
        help="another host's dashboard to merge into /api/fleet and the page, "
        "e.g. --peer hostb=http://192.168.1.20:8787 (repeatable). That host "
        "must be reachable from here, so it needs --host set to something "
        "other than loopback too — same trusted-network caveat as --host. "
        "For a fleet where satellite hosts come and go, --register-to on "
        "each satellite is usually less upkeep than maintaining this by hand.",
    )
    parser.add_argument(
        "--register-to",
        action="append",
        default=[],
        metavar="URL",
        help="a central dashboard to self-register with (repeatable), e.g. "
        "--register-to http://hosta.internal:8787. Requires --advertise-url. "
        "This host shows up in that dashboard's /api/fleet automatically, "
        "expiring DYNAMIC_PEER_TTL (90s) after this stops heartbeating.",
    )
    parser.add_argument(
        "--advertise-url",
        default=None,
        metavar="URL",
        help="URL other hosts should use to reach this dashboard, e.g. "
        "http://hostb.internal:8787 — required with --register-to; not "
        "auto-detected, since guessing the one reachable address among "
        "hostnames/NICs is unreliable (same reasoning as --host being "
        "explicit rather than auto-bound).",
    )
    parser.add_argument("--register-interval", type=float, default=20.0, help="seconds between heartbeats")
    args = parser.parse_args()

    if args.label:
        SELF_LABEL = args.label
    for item in args.peer:
        label, sep, url = item.partition("=")
        label, url = label.strip(), url.rstrip("/").strip()
        if not sep or not label or not url:
            parser.error(f"--peer must be LABEL=URL, got: {item!r}")
        if label == SELF_LABEL:
            parser.error(f"--peer label {label!r} collides with this host's own --label")
        PEERS[label] = url
    if args.register_to and not args.advertise_url:
        parser.error("--register-to requires --advertise-url")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Warning: binding to {args.host} exposes runner logs (may contain workflow "
            "output) to your network. Only do this on a trusted network.",
            file=sys.stderr,
        )

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Runner dashboard ({SELF_LABEL}): http://{args.host}:{args.port}  (Ctrl+C to stop)")
    if PEERS:
        print(f"Static peers: {', '.join(f'{label}={url}' for label, url in PEERS.items())}")
    if args.register_to:
        print(f"Self-registering as {args.advertise_url!r} with: {', '.join(args.register_to)}")
        threading.Thread(
            target=registration_loop,
            args=(args.register_to, args.advertise_url, args.register_interval),
            daemon=True,
        ).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
