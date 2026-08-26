# 02 — Deployment Design

## Host requirements

- Linux x86_64, or Windows x86_64 with Docker Desktop configured for **Linux
  containers**. The runner image and jobs are Linux (`ubuntu-latest`-compatible),
  so Windows containers are not supported in v1.
- Docker Engine (Linux) or Docker Desktop (Windows), plus the `docker compose`
  plugin. Docker Desktop's Linux VM supplies the Docker socket mounted into the
  runner containers.
- Python 3 with PyYAML (`python3 -m pip install -r requirements.txt` on Linux,
  `py -3 -m pip install -r requirements.txt` on Windows) to render Compose.
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

## What's explicitly deferred past v1

- Autoscaling (spin replicas up/down based on queue depth) — static `replicas:`
  count is sufficient at this project's current job volume. Revisit if idle-runner
  cost or queue wait time becomes a problem; see
  [04_ROADMAP.md](04_ROADMAP.md) Phase 3.
- Kubernetes / actions-runner-controller — Compose is enough for one host; only
  worth the operational overhead of a Kubernetes cluster past a scale this project
  isn't at.
- Multi-host pooling — v1 assumes one host is enough capacity; revisit if not.
