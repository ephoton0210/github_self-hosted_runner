# github_self-hosted_runner

A self-hosted GitHub Actions runner fleet, built to stop a private-repo account from
running out of the free 2,000-minutes/month Actions quota — without raising a
spending limit or upgrading a plan.

**Status: Phase 1 (repository-scoped MVP) in progress.** Design is done; the
runner image, registration entrypoint, and declarative repo config are
scaffolded — see [`development/`](development/) for the full plan and
[04_ROADMAP.md](development/04_ROADMAP.md) for what's left.

## Repository layout

| Path | Covers |
|---|---|
| [`docker/runner/Dockerfile`](docker/runner/Dockerfile) | Runner image: pinned `actions/runner` + `docker` CLI (DooD) |
| [`scripts/register-runner.sh`](scripts/register-runner.sh) | Container entrypoint — mints a registration token, runs one ephemeral job, exits |
| [`scripts/render-compose.py`](scripts/render-compose.py) | Renders `config/repos.yaml` into `compose.generated.yaml` |
| [`config/repos.yaml.example`](config/repos.yaml.example) | Template for the declarative repo list — copy to `config/repos.yaml` (gitignored) and fill in real values |

## Why

GitHub Actions' free minutes are billed **per account**, shared across every
private repo you own. A private monorepo with several CI workflows can burn through
that pool in a single large merge — once it hits zero, every job is refused until
the quota resets or you explicitly raise your spending limit above $0. Running jobs
on infrastructure you own sidesteps the quota entirely: GitHub only schedules the
job and relays its logs, it never executes on GitHub's own compute, so self-hosted
runner time is not metered against Actions minutes on any plan.

## Design docs

| Doc | Covers |
|---|---|
| [00_OVERVIEW.md](development/00_OVERVIEW.md) | Problem, goals, non-goals, the personal-account constraint that shapes everything else |
| [01_ARCHITECTURE.md](development/01_ARCHITECTURE.md) | Runner mechanism, repo-scoped vs org-scoped registration, the two-phase topology |
| [02_DEPLOYMENT_DESIGN.md](development/02_DEPLOYMENT_DESIGN.md) | Container image, Docker-outside-of-Docker rationale, declarative repo config, token lifecycle |
| [03_SECURITY.md](development/03_SECURITY.md) | Why this fleet never serves a public repo's `pull_request` workflow, credential handling, blast-radius controls |
| [04_ROADMAP.md](development/04_ROADMAP.md) | Phased plan: repo-scoped MVP → org-scoped account-wide pool → deferred scale work |

## License

[MIT](LICENSE) — use this however is useful to you.
