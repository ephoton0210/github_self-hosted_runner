#!/usr/bin/env python3
"""Local status dashboard for this host's self-hosted runner fleet.

Reads only local state — Docker (Linux container fleet), macOS launchd, and
Windows Scheduled Tasks — never the GitHub API. It shows what start.sh /
start-macos.sh / start-windows.ps1 actually started on *this* host: which
runners exist, whether each is idle, running a job, starting up, or stopped,
and a tail of each runner's own log. A repo's fleet may span multiple hosts;
each host's dashboard only sees its own.

Usage: scripts/dashboard.py [--host 127.0.0.1] [--port 8787]
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import yaml

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

    runners = []
    for service_name, service in services.items():
        env = service.get("environment", {})
        owner = env.get("GH_OWNER", "?")
        repo = env.get("GH_REPO", "?")
        runner_name = env.get("RUNNER_NAME", service_name)
        labels = env.get("RUNNER_LABELS", "")
        container = live.get(service_name)

        if container is None or container.get("State") != "running":
            state, detail = "stopped", (container.get("Status") if container else "not created")
        else:
            logs = run(["docker", "logs", "--tail", "60", service_name], cwd=PROJECT_ROOT) or ""
            state, detail = infer_state(logs.splitlines())
            if state == "unknown":
                detail = container.get("Status", "")

        runners.append(
            {
                "id": f"docker:{service_name}",
                "kind": "linux-docker",
                "name": runner_name,
                "repo": f"{owner}/{repo}",
                "labels": labels,
                "state": state,
                "detail": detail,
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
            }
        )

    return {"available": True, "runners": runners}


def build_status() -> dict:
    return {
        "host": platform.node(),
        "os": platform.system(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
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
  main { padding: 16px 24px; max-width: 1100px; margin: 0 auto; }
  section { margin-bottom: 24px; }
  section h2 { font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin: 0 0 8px; }
  .empty { color: var(--muted); padding: 12px; border: 1px dashed var(--border); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
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
let openLogId = null;
let logTimer = null;

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

function badge(state) {
  const cls = STATE_LABELS[state] ? state : "unknown";
  return `<span class="badge ${cls}"><span class="dot"></span>${STATE_LABELS[cls]}</span>`;
}

function renderRunnerRow(r) {
  const id = escapeHtml(r.id);
  return `<tr class="runner-row" data-id="${id}">
    <td>${escapeHtml(r.name)}</td><td>${escapeHtml(r.repo)}</td><td>${badge(r.state)}</td>
    <td class="detail">${escapeHtml(r.detail || "")}</td>
  </tr>
  <tr class="log-row" data-log-for="${id}"><td colspan="4"><div class="log-panel" id="log-${cssId(r.id)}">
    <div class="log-head"><span>tail -f (this runner's own log)</span><span class="log-updated"></span></div>
    <pre class="log-body">loading...</pre>
  </div></td></tr>`;
}

function cssId(id) { return id.replace(/[^a-zA-Z0-9_-]/g, "_"); }

function renderSection(key, data) {
  const title = SECTION_TITLES[key];
  if (!data.available) {
    return `<section><h2>${title}</h2><div class="empty">${data.reason}</div></section>`;
  }
  if (data.runners.length === 0) {
    return `<section><h2>${title}</h2><div class="empty">No runners rendered yet for this fleet.</div></section>`;
  }
  const rows = data.runners.map(renderRunnerRow).join("");
  return `<section><h2>${title}</h2><table>
    <thead><tr><th>Name</th><th>Repo</th><th>State</th><th>Detail</th></tr></thead>
    <tbody>${rows}</tbody></table></section>`;
}

async function refreshStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  document.getElementById("meta").textContent = `${data.host} (${data.os}) — updated ${new Date(data.generated_at).toLocaleTimeString()}`;
  const main = document.getElementById("main");
  main.innerHTML = Object.entries(data.sections).map(([k, v]) => renderSection(k, v)).join("");
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
    openLogId = null;
    clearInterval(logTimer);
    return;
  }
  if (openLogId) {
    const prev = document.getElementById(`log-${cssId(openLogId)}`);
    if (prev) prev.classList.remove("open");
  }
  openLogId = id;
  panel.classList.add("open");
  clearInterval(logTimer);
  refreshLog();
  logTimer = setInterval(refreshLog, 2000);
}

async function refreshLog() {
  if (!openLogId) return;
  const res = await fetch(`/api/logs?id=${encodeURIComponent(openLogId)}&tail=200`);
  const data = await res.json();
  const panel = document.getElementById(`log-${cssId(openLogId)}`);
  if (!panel) return;
  const body = panel.querySelector(".log-body");
  const wasAtBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 20;
  body.textContent = data.lines.join("\\n");
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
        elif parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            runner_id = (query.get("id") or [""])[0]
            try:
                tail = max(1, min(2000, int((query.get("tail") or ["200"])[0])))
            except ValueError:
                tail = 200
            self._send_json({"id": runner_id, "lines": fetch_log(runner_id, tail)})
        else:
            self._send_json({"error": "not found"}, status=404)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1, loopback-only)")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"Warning: binding to {args.host} exposes runner logs (may contain workflow "
            "output) to your network. Only do this on a trusted network.",
            file=sys.stderr,
        )

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Runner dashboard: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
