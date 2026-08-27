# 03 — Security

Self-hosted runners trade GitHub's throwaway-VM isolation for your own
infrastructure's trust boundary. The controls below exist because the failure mode
here is not "a build fails" — it's "arbitrary code from a workflow run had access to
the host that also builds and pushes your other private repos' images."

## The one rule that matters most: never serve `pull_request` on a public repo

This project's own repository (`github_self-hosted_runner`) is **public**. If a
public repo's `pull_request`-triggered workflow ever runs on a self-hosted runner,
anyone who opens a fork PR can get arbitrary code execution on that runner's host —
this is GitHub's own documented warning, not a hypothetical.

Consequence for this project specifically:

- This runner fleet serves **private** target repos only. Do not add a public repo
  to `config/repos.yaml` / the Phase-2 runner group.
- This repo's *own* CI (linting the Compose files, validating `repos.yaml`, etc.)
  must run on **GitHub-hosted** runners, precisely because it is public and its
  `pull_request` workflows can be triggered by anyone's fork. Do not "dogfood" the
  fleet on its own repo.
- If a target repo's visibility is ever flipped to public, remove it from
  `config/repos.yaml` in the same change, before the visibility flip merges.

## Ephemeral by default

Every runner container is single-job (`--ephemeral`, see
[02_DEPLOYMENT_DESIGN.md](02_DEPLOYMENT_DESIGN.md)). This bounds the blast radius of
a compromised or malicious job to one container lifetime — nothing persists to be
inherited by the next job, and there's no long-lived credential cached inside a
runner process across jobs.

The native macOS implementation applies the same one-job lifecycle, but it is not
a security sandbox: workflow processes run directly as the macOS launch-agent
user. It removes the expanded runner and `_work` directories between jobs; it
cannot undo changes a job makes elsewhere in that user's account, Keychain, or the
host. Use a dedicated, non-administrator macOS account with only the signing keys,
certificates, and toolchains required by these trusted private workflows.

## Docker socket exposure (DooD) is host-level trust, not sandboxing

The chosen DooD approach (mounting `/var/run/docker.sock`) means any job on this
fleet can, in principle, control the host's Docker daemon — start privileged
containers, read other containers' volumes, etc. This is accepted specifically
*because* every workflow that runs here is defined inside a private repo, editable
only by trusted collaborators, never by an anonymous fork PR (see rule above). If
that trust assumption ever changes for a given target repo, that repo must move
back to GitHub-hosted runners, not stay on this fleet with DinD as a patch — DinD
narrows the blast radius but does not make an untrusted-contributor workflow safe
to run here.

## Credentials

- **Registration PAT**: fine-grained personal access token, `Administration: write`
  permission, scoped to exactly the repos in `config/repos.yaml` (Phase 1) or the
  target org (Phase 2). Not a classic PAT with blanket `repo` scope. Rotated on a
  schedule; never committed; injected via Compose `env_file` / Docker secrets only.
- **Workflow secrets** (e.g. `GITHUB_TOKEN` for GHCR push): unchanged from
  GitHub-hosted behavior — GitHub still injects these per-job over the same
  connection; the runner host never needs its own copy of a target repo's secrets
  beyond the registration PAT above.
- No secret is ever baked into the runner image.

## Network egress

Host firewall should allow-list only what's needed: `github.com`,
`*.actions.githubusercontent.com`, `ghcr.io`, and the package registries the target
repos actually pull from (PyPI, npm) — not open egress. Reduces the value of the
host as a pivot point even if a job is compromised.

## Resource isolation

Per-container CPU/memory limits in the Compose file, sized so one repo's heavy
Docker-image-build job cannot starve another repo's concurrently-running job on the
same host. Tuned during Phase 1 rollout against real job resource usage, not
guessed upfront.

## Update cadence

- `actions/runner` version pinned explicitly in `docker/runner/Dockerfile`; bumped
  on a monthly check against upstream releases, not auto-pulled `:latest`.
- Native macOS runner version and SHA-256 pinned explicitly in
  `scripts/macos-runner-loop.sh`; bumped in the same change after verifying the
  official release checksums. Its automatic updater stays disabled so a known
  archive is what launches.
- Base OS image patched on the same cadence.
- Any CVE affecting the pinned runner version is an out-of-band bump, not held for
  the monthly cycle.
