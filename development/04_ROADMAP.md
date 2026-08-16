# 04 — Roadmap

## Phase 0 — Design (this document set)

Status: **done**. Deliverables: [00_OVERVIEW.md](00_OVERVIEW.md),
[01_ARCHITECTURE.md](01_ARCHITECTURE.md),
[02_DEPLOYMENT_DESIGN.md](02_DEPLOYMENT_DESIGN.md),
[03_SECURITY.md](03_SECURITY.md), this roadmap.

## Phase 1 — Repository-scoped MVP

Status: not started.

Scope: one host, repository-scoped runners (per
[01_ARCHITECTURE.md](01_ARCHITECTURE.md) Phase 1), serving `bvSkill` first, then
`oneTest` and `stockConn`.

Work items:

1. `docker/runner/Dockerfile` — pinned `actions/runner` + `docker` CLI (DooD).
2. `scripts/register-runner.sh` — entrypoint: mint registration token via
   fine-grained PAT, register ephemeral, run one job, exit.
3. `config/repos.yaml` + `scripts/render-compose.py` — declarative repo list →
   generated Compose file (see [02_DEPLOYMENT_DESIGN.md](02_DEPLOYMENT_DESIGN.md)).
4. Point `bvSkill`'s heaviest workflow jobs (the `build-backend` / `build-frontend`
   / `build-postgres` Docker-image jobs across its platform `*-build-check.yml`
   workflows) at `runs-on: [self-hosted, linux, x64, docker]`; leave lightweight
   typecheck/lint jobs on `ubuntu-latest` initially to de-risk the cutover.
5. Validate end-to-end on a real PR: checks appear normally, logs stream, GHCR push
   still works from the runner's Docker context.
6. Size `replicas:` per repo against observed concurrency (the 4-way
   `backend-check` fan-out is the concrete baseline).
7. Run for 2–4 weeks alongside GitHub-hosted as a fallback; confirm zero
   GitHub-hosted minutes consumed for migrated jobs via the Billing → Metered usage
   page.

Acceptance criteria: a merge as large as the one that originally exhausted the
2,000-minute quota completes without touching GitHub-hosted minutes for any
migrated job.

## Phase 2 — Organization-scoped, true account-wide pool

Status: not started. Depends on: Phase 1 acceptance criteria met.

Work items:

1. Create (or convert to) a free GitHub Organization.
2. Decide per-repo: transfer ownership into the org, or leave as-is and only put
   *new* repos in the org — transferring existing repos touches collaborator
   permissions, any hard-coded `github.com/ephoton0210/...` URLs, and submodule
   references (`bvSkill` pulls `oneTest`/`stockConn` as submodules — those URLs
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
