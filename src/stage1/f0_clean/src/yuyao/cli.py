"""Command-line entry point. `[project.scripts]` in `pyproject.toml` points at `main` here."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
