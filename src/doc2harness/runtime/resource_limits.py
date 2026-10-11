"""Resource caps for containers that run artifact code.

An artifact is untrusted code by construction -- it is the thing under test. It
gets executed by almost every driver, and without a cap a single runaway
allocation can exhaust the host and take every concurrent run down with it.

Why this belongs in the framework rather than in whoever launches the run: the
cap has to hold no matter which driver is executing, including drivers added
later. A per-caller convention would be forgotten exactly once and cost a host.

The cap is deliberately generous. A scored container normally sits in the tens of
MB, so the default is far above ordinary work; it exists to contain a runaway,
not to ration. `SBH_CONTAINER_MEMORY=0` lifts it for debugging an artifact that
genuinely needs more.
"""
from __future__ import annotations

import os

DEFAULT_MEMORY = "12g"


def container_memory_limit() -> str:
    """The configured cap, or '' when disabled."""
    return str(os.environ.get("SBH_CONTAINER_MEMORY", DEFAULT_MEMORY) or "").strip()


def container_memory_flags() -> list[str]:
    """`docker run` flags capping one container's memory. Empty list = uncapped.

    `--memory-swap` is set equal to `--memory` on purpose: with swap available the
    cgroup degrades into host-wide reclaim before it kills anything, and that
    stall is what turns one runaway into a whole-batch failure. Equal values make
    the kernel kill the offender promptly, inside its own cgroup.
    """
    value = container_memory_limit()
    if value in ("", "0", "none"):
        return []
    return ["--memory", value, "--memory-swap", value]
