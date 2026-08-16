# 04 — Roadmap

## Phase 0 — Design (this document set)

Status: **done**. Deliverables: [00_OVERVIEW.md](00_OVERVIEW.md),
[01_ARCHITECTURE.md](01_ARCHITECTURE.md),
[02_DEPLOYMENT_DESIGN.md](02_DEPLOYMENT_DESIGN.md),
[03_SECURITY.md](03_SECURITY.md), this roadmap.

## Phase 1 — Repository-scoped MVP

Status: **in progress**. Items 1–3 (buildable without a live host or a real
target repo) are scaffolded; items 4–7 need an actual host and a real target
repo to proceed.

Scope: one host, repository-scoped runners (per
[01_ARCHITECTURE.md](01_ARCHITECTURE.md) Phase 1), serving one target repo first,
then expanding to the rest of the private repos hitting the quota.

Work items:

1. ~~`docker/runner/Dockerfile` — pinned `actions/runner` + `docker` CLI (DooD).~~ done.
2. ~~`scripts/register-runner.sh` — entrypoint: mint registration token via
   fine-grained PAT, register ephemeral, run one job, exit.~~ done.
3. ~~`config/repos.yaml` + `scripts/render-compose.py` — declarative repo list →
   generated Compose file (see [02_DEPLOYMENT_DESIGN.md](02_DEPLOYMENT_DESIGN.md)).~~
   done — copy `config/repos.yaml.example` to `config/repos.yaml` (gitignored,
   holds real repo names) and run `scripts/render-compose.py`.
4. Point the first target repo's heaviest workflow jobs (e.g. Docker-image build
   jobs) at `runs-on: [self-hosted, linux, x64, docker]`; leave lightweight
   typecheck/lint jobs on `ubuntu-latest` initially to de-risk the cutover.
5. Validate end-to-end on a real PR: checks appear normally, logs stream, registry
   push still works from the runner's Docker context.
6. Size `replicas:` per repo against observed concurrency (a multi-way job fan-out
   on a real PR is the concrete baseline to size against).
7. Run for 2–4 weeks alongside GitHub-hosted as a fallback; confirm zero
   GitHub-hosted minutes consumed for migrated jobs via the Billing → Metered usage
   page.

Acceptance criteria: a merge as large as the one that originally exhausted the
2,000-minute quota completes without touching GitHub-hosted minutes for any
migrated job.

## Phase 2 — Organization-scoped, true account-wide pool

Status: not started. Depends on: Phase 1 acceptance criteria met.

Work items:

1. Identify or create a free GitHub Organization to act as the shared pool's home
   (see [01_ARCHITECTURE.md](01_ARCHITECTURE.md) Phase 2 — an existing,
   lightly-used org may already qualify).
2. Decide per-repo: transfer ownership into the org, or leave as-is and only put
   *new* repos in the org — transferring existing repos touches collaborator
   permissions, any hard-coded `github.com/<owner>/...` URLs, and submodule
   references (if a target repo pulls other target repos as submodules, those URLs
   would need updating on transfer).
3. Register the Phase 1 runner fleet's containers against the org instead of
   per-repo (registration-target + config schema change only, per
   [02_DEPLOYMENT_DESIGN.md](02_DEPLOYMENT_DESIGN.md) — no image/mechanism change).
4. Create a runner group with an access policy listing the org's member repos.
5. Decommission the Phase 1 per-repo registrations once the org-level pool is
   confirmed working.

Acceptance criteria: a new repo added to the org gains access to the runner fleet
via a runner-group policy change alone — no new runner registration, no Compose
edit.

## Phase 3 — Scale & observability (deferred, revisit only if needed)

Not scheduled. Trigger conditions to revisit:

- Idle-runner resource cost becomes noticeable → add basic autoscaling
  (queue-depth-triggered replica count, not a full ARC/Kubernetes migration unless
  volume genuinely warrants it).
- Runner registration-token refresh silently failing becomes a recurring incident →
  add alerting (e.g. a simple healthcheck + notification, not a full metrics stack)
  rather than discovering it via a stuck PR check.
- Job queue wait time becomes visible to developers → dashboard for
  runner utilization/queue depth.

## Explicitly out of scope (not "later," just not this project)

- General-purpose CI platform features unrelated to the minutes problem.
- GHCR storage/bandwidth quota management — separate problem, separate quota.
- Windows/macOS self-hosted runners — no target repo currently needs a non-Linux
  runner; revisit only if one does.
