# 00 — Overview

## Problem

GitHub Actions free minutes are metered **per billing account**, not per repository.
A personal GitHub account on the Free plan gets 2,000 minutes/month of GitHub-hosted
runner time, shared across *every* private repository that account owns.

This project exists because that pool is easy to exhaust in a private monorepo with
several parallel CI pipelines: a single large merge that touches multiple product
paths can fan out into many concurrent jobs (type-checks, test suites, and — most
expensive — multi-service Docker image builds), burning the monthly allotment in
minutes rather than weeks. Once the quota hits zero, GitHub does not silently start
billing you (unless you've raised your spending limit above $0) — every subsequent
job is simply refused with *"recent account payments have failed or your spending
limit needs to be increased."* CI stops working until the quota resets or the
spending limit is raised.

Two off-the-shelf fixes exist — raise the spending limit and pay per minute, or
upgrade the account plan — but neither removes the cap, they just move it. This
project takes the third option: run the jobs on infrastructure you own, which GitHub
does not meter at all.

## Goal

Stand up a small, low-maintenance fleet of **self-hosted GitHub Actions runners**
that:

- Removes GitHub-hosted-runner minute consumption entirely for the repos it serves.
- Keeps the exact same developer experience: PR checks, live log streaming, status
  badges — indistinguishable from GitHub-hosted runners on the GitHub UI side.
- Starts from a single physical/virtual host (Phase 1) and grows into a design that
  can genuinely serve *every* repository under one account (Phase 2), not just one
  repo at a time.
- Is documented and packaged well enough that someone outside this project can clone
  it, point it at their own repos, and reuse it — hence public + MIT.

## Non-goals

- This is not a general-purpose CI platform or a Kubernetes-operator replacement for
  [actions-runner-controller](https://github.com/actions/actions-runner-controller).
  Large orgs with heavy elastic scaling needs should use that instead; see
  [04_ROADMAP.md](04_ROADMAP.md) for where this project intentionally stops.
- This does not address GHCR (container registry) storage/bandwidth limits — that is
  a separate quota from Actions minutes.
- This does not cover GitHub Enterprise Cloud-specific runner-group features that
  require an Enterprise account.

## Key constraint that shapes this whole design

GitHub self-hosted runners can be registered at exactly one of three scopes:
**repository**, **organization**, or **enterprise**. A *personal user account* has
no "account-wide" registration scope — only the repository scope is available to
it. Organization-level runner groups (which *do* give you one shared runner pool
across many repos) require the repositories to live inside a GitHub Organization,
which personal accounts are not.

This means "handle the whole account's GitHub Actions" cannot be solved with a
single runner registration while the target repos remain under a personal user
account. The design below is split into two phases specifically because of this:
Phase 1 works today, under a personal account, at the cost of one runner
registration per repo; Phase 2 removes that limitation by moving into a (free)
GitHub Organization. See [01_ARCHITECTURE.md](01_ARCHITECTURE.md) for the full
scope comparison.

## Audience

Anyone hitting the same wall: a personal or small-team GitHub account, one or more
private repositories, and a CI setup that regularly exceeds the free Actions minutes
tier. No GitHub Enterprise subscription assumed.
