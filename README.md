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
| [`start.sh`](start.sh) | One-command launcher: renders compose config and starts runners |
| [`stop.sh`](stop.sh) | Gracefully shuts down runner containers |
| [`docker/runner/Dockerfile`](docker/runner/Dockerfile) | Runner image: pinned `actions/runner` + `docker` CLI (DooD) |
| [`scripts/register-runner.sh`](scripts/register-runner.sh) | Container entrypoint — mints a registration token, runs one ephemeral job, exits |
| [`scripts/render-compose.py`](scripts/render-compose.py) | Renders `config/repos.yaml` into `compose.generated.yaml` |
| [`config/repos.yaml.example`](config/repos.yaml.example) | Template for the declarative repo list — copy to `config/repos.yaml` (gitignored) and fill in real values |

## Runner Environment & OS Specifications

The self-hosted runner container is built to match GitHub's official `ubuntu-latest` environment:

| Specification | Details |
|---|---|
| **Operating System** | **Ubuntu 24.04 LTS (`noble`)**, 64-bit x86 (`x64`) — matches GitHub-hosted `ubuntu-latest` |
| **Runner Runtime** | Official GitHub Actions Runner runtime (pinned version in [`Dockerfile`](docker/runner/Dockerfile)) |
| **Pre-installed Tooling** | `docker-ce-cli`, `git`, `curl`, `jq`, `ca-certificates`, `gnupg` |
| **Docker Support** | **DooD (Docker-outside-of-Docker)** via host `/var/run/docker.sock` bind mount |
| **Build Caching** | Reuses host Docker layer cache across jobs (resulting in faster container builds) |
| **Action Compatibility** | Standard actions (`actions/checkout`, `actions/setup-python`, `actions/setup-node`, `docker/build-push-action`, etc.) work out of the box |


## GitHub PAT Permission Requirements

To allow runner containers to dynamically mint short-lived registration and removal
tokens via the GitHub API, a **Fine-grained Personal Access Token (PAT)** is required.

### Required Permissions

Go to GitHub **Settings** → **Developer Settings** → **Personal access tokens** → **[Fine-grained tokens](https://github.com/settings/tokens?type=beta)**:

| Scope / Setting | Recommended Value | Description / Purpose |
|---|---|---|
| **Resource owner** | Target personal account or org | The owner of the target repositories |
| **Repository access** | **Only select repositories** | Select the exact private repositories listed in `config/repos.yaml` |
| **Repository permissions** → **Administration** | **Read and write** | **Mandatory.** Required to invoke GitHub Actions runner registration (`/actions/runners/registration-token`) and removal (`/actions/runners/remove-token`) endpoints. |

### Why `Administration` instead of `Actions`?

- **Infrastructure vs. Workflow**: The `Actions` permission only controls running, inspecting, and canceling workflows. In GitHub's security model, registering or removing compute nodes (Runners) in a repository is an **infrastructure management action (Repository Settings)**, restricted exclusively to repository administrators.
- **Security Boundary**: A self-hosted runner executes code directly on your local infrastructure (with Docker socket access). To prevent unauthorized computing nodes from being attached to a repo, GitHub strictly restricts runner registration to `Administration: write`.

> [!IMPORTANT]
> **Troubleshooting HTTP 404:**
> If your PAT does not have `Administration: write` permissions, or if the target repo is not selected in the token's repository access list, GitHub's API will return **`HTTP 404 Not Found`** (instead of `403 Forbidden`) to prevent repository discovery. Ensure both settings are configured properly.

## Quickstart

### 1. Configure Credentials
Copy `.env.example` to `.env` and fill in your Fine-grained PAT:
```bash
cp .env.example .env
```
```ini
GH_PAT=github_pat_xxxxxxxxxxxxxxxxxxxx
```

### 2. Configure Target Repositories
Copy `config/repos.yaml.example` to `config/repos.yaml`:
```bash
cp config/repos.yaml.example config/repos.yaml
```
Define your target repositories, labels, and concurrency replicas:
```yaml
repos:
  - owner: <account>
    repo: repo-a
    labels: [self-hosted, linux, x64, docker]
    replicas: 2
```

### 3. Start Runners
```bash
./start.sh
```
Check live logs to confirm runners are connected and `Listening for Jobs`:
```bash
docker compose -f compose.generated.yaml logs -f
```

To stop all runners:
```bash
./stop.sh
```

### 4. Update Target Repository Workflows
In the target repository (e.g. `your-repo/.github/workflows/*.yml`), switch jobs to target self-hosted runners:
```yaml
runs-on: [self-hosted, linux, x64]
# or simply:
runs-on: self-hosted
```

> [!WARNING]
> **Do not use "Re-run" on old failed workflow runs:**
> GitHub Actions' "Re-run jobs" button executes the exact workflow configuration from the time that commit was created. If the historical commit used `runs-on: ubuntu-latest`, re-running it will still request GitHub-hosted compute and fail immediately when your account quota is 0.
> 
> To test your self-hosted runners, you must **commit and push** the updated workflow file (or open a new PR / dispatch a manual workflow) to create a **new workflow run**.

## How to Verify (Confirming Success)

You can verify that your runners are properly registered and working at three levels:

### 1. Local Container Logs
Check the live runner logs:
```bash
docker compose -f compose.generated.yaml logs -f
```
A successful runner startup displays:
```text
√ Connected to GitHub
√ Runner successfully added
√ Settings Saved.
Current runner version: '2.336.0'
YYYY-MM-DD HH:MM:SSZ: Listening for Jobs
```
When you see **`Listening for Jobs`**, the runner is authenticated and standing by for CI jobs.

### 2. GitHub Web UI (Repository Settings)
Navigate to your target repository settings in a browser:
```text
https://github.com/<owner>/<repo>/settings/actions/runners
```
- **Status Badge**: You should see your runner instances (e.g. `runner-<repo>-1`, `runner-<repo>-2`) listed with a green **`Idle`** badge.
- **Labels**: Verify that the labels (e.g. `self-hosted`, `linux`, `x64`, `docker`) match your `config/repos.yaml`.

### 3. Workflow Execution
When a workflow job with `runs-on: [self-hosted, ...]` is triggered:
- **GitHub UI**: The runner status changes from **`Idle`** to **`Active`**.
- **Container Logs**: You will see `Running job: <job-name>` followed by step execution logs.
- **Lifecycle**: Upon job completion, the container exits (`--ephemeral`), and Docker Compose automatically spins up a fresh container instance with a new registration token to wait for the next job.

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
