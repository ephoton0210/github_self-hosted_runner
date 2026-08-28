# 02 — Deployment Design

## Host requirements

- Linux x86_64, or Windows x86_64/ARM64 with Docker Desktop configured for **Linux
  containers**. The runner image and jobs are Linux (`ubuntu-latest`-compatible),
  so Windows containers are not supported for this fleet — see "Native Windows
  runners" below for jobs that need Windows itself, not a Linux container on a
  Windows host.
- Or macOS 11+ on Apple silicon or Intel for workflows that need native Apple
  tooling. macOS jobs run as a user-level launchd agent, not in a Linux container.
- Or Windows x86_64/ARM64 for workflows that need native Windows tooling a Linux
  container cannot supply. Windows jobs run under a Windows Scheduled Task as the
  logged-in user, not in a container; this is independent of, and can run
  alongside, the Docker Desktop Linux-container path above.
- Docker Engine (Linux) or Docker Desktop (Windows), plus the `docker compose`
  plugin, for the Linux container fleet. Docker Desktop's Linux VM supplies the
  Docker socket mounted into the runner containers. Not required for a
  Windows-only `windows:` fleet.
- Python 3 with PyYAML (`python3 -m pip install -r requirements.txt` on Linux,
  `py -3 -m pip install -r requirements.txt` on Windows) to render Compose or the
  native macOS/Windows agent configuration.
- PowerShell 7+ (`pwsh.exe`) on the Windows host, for a native `windows:` fleet —
  the Scheduled Task runs `windows-runner-loop.ps1` under `pwsh.exe`, not the
  Windows PowerShell 5.1 that ships in-box.
- A checkout that preserves LF line endings for Linux-executed sources. The
  repository's `.gitattributes` enforces this for shell scripts, the Dockerfile,
  and Python renderer on Windows.
- Outbound HTTPS (443) to `github.com`, `*.actions.githubusercontent.com`, and
  `ghcr.io` — no inbound ports required anywhere in this design.
- Persistent disk for the Docker build-layer cache (this is what makes self-hosted
  builds *faster* than GitHub-hosted, not just free — see "Build cache" below).
- Enough RAM/CPU to run the target repos' heaviest job (e.g. multi-service Docker
  image builds) without starving concurrent jobs — sized in
  [04_ROADMAP.md](04_ROADMAP.md) Phase 1 acceptance criteria.

## Runner image

Base image: the official `actions/runner` release tarball inside a minimal
container, with `docker` CLI installed (not a full Docker daemon — see
"Docker-in-Docker vs Docker-outside-of-Docker" below). Built from this repo's
`docker/runner/Dockerfile`, not pulled from a third-party image, so the supply
chain is auditable end-to-end (`actions/runner` version pinned explicitly, rebuilt
and re-pulled on a schedule per [03_SECURITY.md](03_SECURITY.md)).

Every runner container is started with `--ephemeral` (`RUNNER_EPHEMERAL=1`): it
accepts exactly one job, executes it, reports the result, then exits. Compose (or a
process supervisor) restarts the container to pick up the next job. This trades a
few seconds of container-start latency per job for perfect isolation and zero
state-drift — acceptable for this project's job sizes.

## Native macOS runners

An optional `macos:` mapping on an entry in `config/repos.yaml` creates a separate
native fleet for that repository:

```
  - owner: <account>
    repo: apple-app
    labels: [self-hosted, linux, x64]
    replicas: 1
    macos:
      labels: [native-macos]    # additional, custom routing label
      runner_name_prefix: runner-apple-app-macos-host-a
      replicas: 1
```

`start-macos.sh` invokes `scripts/render-macos-launchd.py`, then bootstraps the
generated user agents in the current GUI login session. The existing repo-level
PAT is read from `.env`, never placed in the plist. Every agent runs
`macos-runner-loop.sh`, which verifies the pinned official archive's SHA-256,
expands a fresh runner directory, registers it with `--ephemeral --disableupdate`,
and erases that directory after its one job. The cached archive and local logs stay
under `.runner-macos/`, which is gitignored.

GitHub automatically adds `self-hosted`, `macOS`, and `ARM64`/`x64` labels. The
`macos.labels` list supplies extra custom labels only; in the example, workflows
should use `runs-on: [self-hosted, macOS, native-macos]`. Do not add a `macos:` mapping to
a public repository or to any repository where untrusted pull requests can execute.

## Native Windows runners

An optional `windows:` mapping on an entry in `config/repos.yaml` creates a
separate native fleet for that repository, mirroring the `macos:` mechanism above:

```
  - owner: <account>
    repo: dotnet-app
    labels: [self-hosted, linux, x64]
    replicas: 1
    windows:
      labels: [native-windows]    # additional, custom routing label
      runner_name_prefix: runner-dotnet-app-windows-host-a
      replicas: 1
```

`start-windows.ps1` invokes `scripts/render-windows-scheduled-task.py`, then
registers the generated Scheduled Tasks under the `\GitHubSelfHostedRunner\` task
folder and starts them. Task Scheduler's logon trigger plus a restart-on-failure
policy stand in for `launchd`'s `KeepAlive`. The existing repo-level PAT is read
from `.env` at runner-loop start time, never placed in the task XML. Every task
runs `windows-runner-loop.ps1` under `pwsh.exe`, which verifies the pinned official
archive's SHA-256, expands a fresh runner directory, registers it with
`--ephemeral --disableupdate`, and erases that directory after its one job.
Stopping is cooperative: `stop-windows.ps1` (and a `start-windows.ps1` re-run that
drops a replica) drops a `stop.flag` file the loop polls for between and during
jobs, then unregisters the task once it reports non-`Running`. The cached archive
and local logs stay under `.runner-windows\`, which is gitignored.

GitHub automatically adds `self-hosted`, `Windows`, and `X64`/`ARM64` labels. The
`windows.labels` list supplies extra custom labels only; in the example, workflows
should use `runs-on: [self-hosted, Windows, native-windows]`. Do not add a
`windows:` mapping to a public repository or to any repository where untrusted pull
requests can execute.

## Docker-in-Docker vs Docker-outside-of-Docker

Most target repos' workflows use `docker/build-push-action` and
`docker/setup-buildx-action`, so the runner needs to be able to build images.
Two ways to give it that ability:

| Approach | How | Trade-off |
|---|---|---|
| **DooD** (Docker-outside-of-Docker) — chosen | Mount the host's `/var/run/docker.sock` into the runner container | Fast (shares host's build cache across all runner containers); the runner container can control the host's Docker daemon — treat it as **host-level trust**, not sandboxed |
| **DinD** (Docker-in-Docker) | Run a nested Docker daemon inside the runner container (`--privileged` or `docker:dind` sidecar) | Better isolation per job; loses cross-job layer-cache reuse unless a cache volume is explicitly shared; typically slower |

**Decision: DooD**, because these are private repos with trusted, non-fork-triggered
workflows (see [03_SECURITY.md](03_SECURITY.md) for why that assumption is load-
bearing) and the build-cache reuse materially matters for the multi-service image
builds this project exists to make cheap. If a future target repo ever needs to run
untrusted/fork-triggered workflows on this fleet, that repo must **not** be added to
this runner pool — full stop; see the security doc.

## Declarative repo list (Phase 1)

Phase 1's registration sprawl (one runner set per repo) is kept manageable with a
single source of truth:

```
config/repos.yaml
  - owner: <account>
    repo: repo-a
    labels: [self-hosted, linux, x64, docker]
    runner_name_prefix: runner-repo-a-host-a  # optional, host-specific GitHub runner name
    replicas: 2        # match observed concurrency, e.g. a multi-way job fan-out
  - owner: <account>
    repo: repo-b
    labels: [self-hosted, linux, x64]
    replicas: 1
  - owner: <account>
    repo: repo-c
    labels: [self-hosted, linux, x64]
    replicas: 1
```

A generator script (`scripts/render-compose.py`) turns this file into a
`compose.generated.yaml` with one Compose service per `(repo, replica)` pair. Adding
a repo is a 3-line YAML edit + `docker compose up -d`, not a manual runbook.
`runner_name_prefix` is optional; use it when another fleet may leave a GitHub
runner session with the default name. It changes only the GitHub-visible runner
name, not the Compose service name or number of replicas.

Because `render-compose.py` runs on the actual host at `start.sh`/`start.ps1` time,
it detects that host's OS and, when neither `runner_name_prefix` is set, names both
the Compose service and the default GitHub-visible runner with a `-windows-docker`
suffix on a Windows host (Docker Desktop) — e.g. `runner-repo-a-windows-docker-1` —
versus the plain `runner-repo-a-1` on a Linux host. This is still the same Linux
container image and mechanism either way; the suffix exists only so a repo served
by both a Linux host and a Windows-Docker-Desktop host doesn't show two
identically-named runners in the GitHub UI, and so the Windows-hosted Linux
containers aren't confused at a glance with a native `windows:` fleet for the same
repo (which defaults to a `-windows` suffix instead).

## Registration-token lifecycle

Runner registration tokens expire in 1 hour, so they cannot be baked into the image
or `.env` file long-term. Each container's entrypoint mints its own token at start
time via the GitHub API:

```
POST /repos/{owner}/{repo}/actions/runners/registration-token
```

using a **fine-grained PAT** scoped to `Administration: write` on only the repos
listed in `config/repos.yaml` (see [03_SECURITY.md](03_SECURITY.md) for why not a
classic PAT). The PAT itself is injected via Docker secrets / compose `env_file`,
never committed, never baked into the image.

## Migration to Phase 2 (org-level)

When Phase 2 begins, the same runner containers switch their registration target
from `/repos/{owner}/{repo}/actions/runners/registration-token` to
`/orgs/{org}/actions/runners/registration-token`, and `config/repos.yaml` collapses
into `config/runner-group.yaml` (replica count only, no per-repo entries — access is
governed by the org's runner-group policy instead). The container image and
Compose/token-refresh mechanics are unchanged; only the registration target and
config schema change. This is intentional — Phase 1 is not thrown away when Phase 2
lands.

## Local monitoring dashboard

`scripts/dashboard.py` (started via `dashboard.sh` / `dashboard.ps1`) is a
single-file, stdlib-only Python HTTP server — no new dependency beyond the
PyYAML already required for the renderers, no build step, no framework. It
binds to `127.0.0.1:8787` by default and serves one HTML page plus three JSON
endpoints (`/api/status`, `/api/fleet`, `/api/logs`) that a small polling
frontend hits every few seconds; there's no WebSocket/SSE layer, matching
this project's bias toward the simplest mechanism that's still responsive
enough for a CI runner's job cadence.

It reads only state already local to the host it runs on, per fleet type:

- **Docker**: `compose.generated.yaml` for the roster (owner/repo/labels/name
  per service), `docker compose ps --format json` for live container state.
- **macOS**: `.runner-macos/launchd/manifest.txt` for the roster, each
  service's rendered `.plist` for owner/repo, `launchctl print` for load
  state.
- **Windows**: `.runner-windows/scheduled-tasks/manifest.txt` for the roster,
  each task's rendered XML for owner/repo, `schtasks /Query` for task state.

For all three, per-runner Idle / Running \<job\> / Starting state comes from
scanning the tail of that runner's own log for the same three literal
markers [03_SECURITY.md](03_SECURITY.md)/README.md's "How to Verify" section
already documents as ground truth: `Listening for Jobs`, `Running job: `,
and `is ready for one job`. This is also why
[`windows-runner-loop.ps1`](../scripts/windows-runner-loop.ps1) writes its
own `.runner-windows/logs/<name>.log` — Task Scheduler has no equivalent of
launchd's `StandardOutPath`, so without that the dashboard (and an operator
tailing logs by hand) would have no Windows-native log to read at all.

### Multi-host aggregation and resource usage

`/api/status` stays local-only and unauthenticated-but-unprefixed — it is the
one thing a peer dashboard actually calls on another host, so its shape
can't change without breaking that. `/api/fleet` is the aggregating view: it
always includes this host's own status (fetched the same way `/api/status`
gets it, not cached) plus one `GET <peer-url>/api/status` per configured
`--peer LABEL=URL`, each on its own short timeout so one unreachable peer
degrades to an "Unreachable" block instead of failing the whole page. Runner
`id`s coming out of `/api/fleet` are rewritten to `LABEL::<original-id>`
(self included) so `/api/logs?id=...` can route: a `SELF_LABEL::` prefix is
handled locally, any other configured peer's is proxied to that peer's own
`/api/logs`, and an unrecognized label fails closed with an explicit error
rather than silently reading nothing. There is still no new central service
and no new protocol — a peer is just another HTTP client of the exact same
endpoints a browser hits.

This is deliberately still not GitHub-API-backed (see
[04_ROADMAP.md](04_ROADMAP.md) Phase 3): no job-queue-wait-time or
utilization-over-time analytics, just what's true on each host right now.

Resource figures come from the cheapest accurate source per fleet type, not a
new dependency (no `psutil`): `docker stats --no-stream --format json` gives
exact per-container CPU%/mem for the Docker fleet; host-level CPU
load/`os.cpu_count()` and memory (`/proc/meminfo` on Linux, `vm_stat`/`sysctl`
on macOS, `wmic` on Windows) apply to every fleet type but aren't per-process.
Native macOS/Windows runners don't have per-process figures yet — doing that
right means resolving the launchd agent's or Scheduled Task's PID down to the
actual `Runner.Listener` child it eventually spawns, which neither
`macos-runner-loop.sh` nor `windows-runner-loop.ps1` currently tracks or
exposes.

## What's explicitly deferred past v1

- Autoscaling (spin replicas up/down based on queue depth) — static `replicas:`
  count is sufficient at this project's current job volume. Revisit if idle-runner
  cost or queue wait time becomes a problem; see
  [04_ROADMAP.md](04_ROADMAP.md) Phase 3.
- Kubernetes / actions-runner-controller — Compose is enough for one host; only
  worth the operational overhead of a Kubernetes cluster past a scale this project
  isn't at.
- Multi-host pooling — v1 assumes one host is enough capacity; revisit if not.
