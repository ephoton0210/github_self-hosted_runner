FROM ubuntu:24.04

# Pinned per development/03_SECURITY.md — bumped on a monthly check against
# upstream releases, not auto-pulled `:latest`.
ARG RUNNER_VERSION=2.336.0
ARG RUNNER_ARCH=x64

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        jq \
        git \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI only — no daemon. DooD: /var/run/docker.sock is bind-mounted at
# runtime (see development/02_DEPLOYMENT_DESIGN.md). Installing just the CLI
# keeps the image from also carrying a dockerd this container never runs.
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /home/runner

RUN curl -fsSL -o actions-runner.tar.gz \
        "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-${RUNNER_ARCH}-${RUNNER_VERSION}.tar.gz" \
    && tar xzf actions-runner.tar.gz \
    && rm actions-runner.tar.gz \
    && ./bin/installdependencies.sh \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/register-runner.sh /usr/local/bin/register-runner.sh
RUN chmod +x /usr/local/bin/register-runner.sh

# The mounted docker.sock is host-owned (usually root/docker group); matching
# that GID per-host is more fragile than accepting DooD's documented
# host-level trust (development/03_SECURITY.md) and running as root.
ENV RUNNER_ALLOW_RUNASROOT=1

ENTRYPOINT ["/usr/local/bin/register-runner.sh"]
