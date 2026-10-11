#!/usr/bin/env python3
"""Local docker helpers for the stage1 build pipeline.

Only what the build path needs: the docker command vector and an async
subprocess runner. Standard library only, so building a product pulls in no
third-party dependency.
"""
from __future__ import annotations

import asyncio
import os

DOCKER = os.environ.get("D2A_DOCKER", "docker").split()


async def _run(*args: str, timeout: float | None = None) -> tuple[int, str, str]:
    """Run one command (arg list, no shell). Returns (rc, stdout, stderr); on
    timeout the process is killed and TimeoutError propagates."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
