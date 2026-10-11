#!/usr/bin/env python3
"""Unattended entrypoint. Invoked directly by `run_agent.sh` and by external callers.
Arguments are specified in `.d2a_spec/AGENT_CONTRACT.md`.

Callers copy this directory verbatim into a clean container and run it as-is: no package
installation beforehand, and an empty PYTHONPATH.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
