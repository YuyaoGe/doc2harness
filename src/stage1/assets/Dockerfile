# stage1 builder image (build sandbox, built once and reused across all builders):
# OpenCode + python/git + the product skeleton (f0).
#
# Bakes the hollowed skeleton straight into /opt/f0: contract sockets only, no kernel
# sources, no pip-installed mini. The builder implements from scratch against the contract
# in /opt/f0/.d2a_spec.
#
# Carries no evaluation scenario content (the scenarios are held out from the build); the
# model reaches the provider only through the gateway (no upstream credentials in the
# container).
#
# build:  docker build -f src/stage1/assets/Dockerfile \
#             -t ghcr.io/yuyaoge/doc2harness-builder:latest src/stage1
#         Tagging it anything else means passing --image or setting D2A_BUILDER_IMAGE,
#         since that tag is run_build.py's default.
# Note: a real build needs network access to install opencode; run_build.py --dry-run does
# not depend on this image.
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates bash unzip ripgrep \
        iptables iproute2 dnsutils \
    && rm -rf /var/lib/apt/lists/*
# ripgrep: OpenCode's built-in glob/grep tools invoke `rg` directly; without it every
# project-wide search fails with `ripgrep execution failed`.
# iptables/iproute2/dnsutils: let run_build.py --egress gateway-only restrict egress inside
# the container to the local gateway only (blocking github/pypi so a builder cannot reach
# outside help). Requires docker run --cap-add=NET_ADMIN.

# OpenCode (official install script -> /root/.opencode/bin/opencode, matching
# run_build.py's OPENCODE_BIN).
# VERSION must be pinned: unpinned, the script queries api.github.com for the latest
# release, and that API is frequently rate-limited inside a build container.
# The version below tracks the npm registry entry opencode-ai/latest.
ARG OPENCODE_VERSION=1.17.0
RUN curl -fsSL https://opencode.ai/install | VERSION=${OPENCODE_VERSION} bash \
    && /root/.opencode/bin/opencode --version

# The hollowed product skeleton (8 files, contract sockets that only
# `raise NotImplementedError`) -> /opt/f0.
COPY f0_clean/ /opt/f0/
RUN find /opt/f0 -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true

WORKDIR /opt/f0
