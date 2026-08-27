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
| [`start.sh`](start.sh) / [`start.ps1`](start.ps1) | One-command launcher for Linux containers from Bash / PowerShell |
| [`stop.sh`](stop.sh) / [`stop.ps1`](stop.ps1) | Gracefully shuts down Linux runner containers from Bash / PowerShell |
| [`start-macos.sh`](start-macos.sh) / [`stop-macos.sh`](stop-macos.sh) | Installs/removes native macOS launchd runner agents |
| [`.gitattributes`](.gitattributes) | Preserves LF line endings for Linux container sources on Windows checkouts |
| [`docker/runner/Dockerfile`](docker/runner/Dockerfile) | Runner image: pinned `actions/runner` + `docker` CLI (DooD) |
| [`scripts/register-runner.sh`](scripts/register-runner.sh) | Container entrypoint — mints a registration token, runs one ephemeral job, exits |
| [`scripts/render-compose.py`](scripts/render-compose.py) | Renders `config/repos.yaml` into `compose.generated.yaml` |
| [`scripts/render-macos-launchd.py`](scripts/render-macos-launchd.py) | Renders optional `macos:` entries into local launchd agent plists |
| [`scripts/macos-runner-loop.sh`](scripts/macos-runner-loop.sh) | Native macOS one-job runner supervisor, download verifier, and cleanup loop |
| [`config/repos.yaml.example`](config/repos.yaml.example) | Template for the declarative repo list — copy to `config/repos.yaml` (gitignored) and fill in real values |

## Runner Environment & OS Specifications

The Linux runner container matches GitHub's official `ubuntu-latest` environment.
The optional macOS route uses native macOS processes because a Linux container
cannot execute macOS workflows:

| Specification | Details |
|---|---|
| **Linux runner OS** | **Ubuntu 24.04 LTS (`noble`)**, 64-bit x86 (`x64`) — matches GitHub-hosted `ubuntu-latest` |
| **macOS runner OS** | Native macOS 11+ on the host's architecture (`ARM64` on Apple silicon, `x64` on Intel) |
| **Runner Runtime** | Official GitHub Actions Runner runtime (pinned version in [`Dockerfile`](docker/runner/Dockerfile)) |
| **Linux pre-installed tooling** | `docker-ce-cli`, `git`, `curl`, `jq`, `ca-certificates`, `gnupg` |
| **Docker Support** | **DooD (Docker-outside-of-Docker)** via host `/var/run/docker.sock` bind mount |
| **Build Caching** | Reuses host Docker layer cache across jobs (resulting in faster container builds) |
| **macOS lifecycle** | User-level `launchd` agents each provision a verified, native, ephemeral runner for one job, then erase its runner/work directory |


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

### macOS host requirements

Native macOS runners are for workflows that genuinely need macOS (for example,
Xcode, code signing, or an Apple-platform build). They are **not** Docker
containers: jobs run as the currently logged-in macOS user. Use a dedicated macOS
account and only route trusted private-repository workflows to it.

- macOS 11 or later, on either Apple silicon or Intel. `start-macos.sh` selects
  the matching official runner archive automatically.
- A logged-in GUI user session. The launcher creates user-level `launchd` agents;
  do not run it with `sudo`.
- Python 3 with PyYAML (`python3 -m pip install -r requirements.txt`), plus the
  built-in `curl`, `jq`, `tar`, `shasum`, and `launchctl` commands.
- Xcode and any platform-specific toolchains required by the target workflows.
  Docker Desktop is optional and only needed by macOS jobs that invoke `docker`.

The macOS supervisor disables automatic runner updates so its downloaded archive
remains reproducible and SHA-256 verified. Update the pinned version and checksum
together during the monthly runner update in [03_SECURITY.md](development/03_SECURITY.md).

### Windows host requirements

The runners remain **Linux containers** even when the host is Windows. Install
[Docker Desktop](https://www.docker.com/products/docker-desktop/) and make sure it
is running in **Linux containers** mode (the default). Docker Desktop provides the
Linux Docker engine and forwards its Docker socket into the runner containers, so
workflows should continue to request the `linux` labels configured below.
Keep the committed `.gitattributes` file: it prevents Git for Windows from
converting the Linux container entrypoint to CRLF line endings.

Install Python 3, then install the renderer dependency from PowerShell:

```powershell
py -3 -m pip install -r requirements.txt
```

> [!IMPORTANT]
> Docker socket access gives every job host-level control of Docker Desktop's Linux
> VM. Only use this fleet for trusted workflows from private repositories; the same
> security restrictions in [03_SECURITY.md](development/03_SECURITY.md) apply on
> Windows.

### 1. Configure Credentials
Copy `.env.example` to `.env` and fill in your Fine-grained PAT:
```bash
cp .env.example .env
```
On Windows PowerShell:
```powershell
Copy-Item .env.example .env
```
```ini
GH_PAT=github_pat_xxxxxxxxxxxxxxxxxxxx
```

### 2. Configure Target Repositories
Copy `config/repos.yaml.example` to `config/repos.yaml`:
```bash
cp config/repos.yaml.example config/repos.yaml
```
On Windows PowerShell:
```powershell
Copy-Item config/repos.yaml.example config/repos.yaml
```
Define your target repositories, labels, and concurrency replicas:
```yaml
repos:
  - owner: <account>
    repo: repo-a
    labels: [self-hosted, linux, x64, docker]
    # Optional: avoids name clashes with a separate fleet serving this repo.
    runner_name_prefix: runner-repo-a-host-a
    replicas: 2
    # Optional native macOS runners for this same repo. GitHub adds the standard
    # self-hosted, macOS, and ARM64/x64 labels automatically.
    macos:
      labels: [native-macos]
      runner_name_prefix: runner-repo-a-macos-host-a
      replicas: 1
```

### 3. Start Runners
```bash
./start.sh
```
On Windows PowerShell:
```powershell
.\start.ps1
```
On macOS, start only the optional `macos:` runner entries:
```bash
./start-macos.sh
```
Check live logs to confirm runners are connected and `Listening for Jobs`:
```bash
docker compose -f compose.generated.yaml logs -f
```
For native macOS runners:
```bash
tail -f .runner-macos/logs/*.log
```

To stop all runners:
```bash
./stop.sh
```
On Windows PowerShell:
```powershell
.\stop.ps1
```
On macOS:
```bash
./stop-macos.sh
```

### 4. Update Target Repository Workflows
In the target repository (e.g. `your-repo/.github/workflows/*.yml`), switch jobs to target self-hosted runners:
```yaml
runs-on: [self-hosted, linux, x64]
# or simply:
runs-on: self-hosted
```
For a native macOS entry with the example custom `native-macos` label:
```yaml
runs-on: [self-hosted, macOS, native-macos]
```
Add `ARM64` or `x64` only when the workflow must target that exact architecture.

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
The macOS supervisor instead writes `runner-… is ready for one job` to its local
launchd log before the native runner starts listening.

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
