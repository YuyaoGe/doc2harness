"""Bridge the YAML launch boundary to the reviewed production catalog runner.

The generic YAML launcher owns validation and per-model scheduling.  This
adapter owns only the explicit argument translation needed by the production
catalog executor; it never reads previously recorded verdicts.  The deterministic model stimulus ships inside the package, so
running a scenario needs nothing beyond the artifact under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("${"):
        raise SystemExit(f"SBH production executor requires {name}")
    return value


def _positive_int(name: str, default: int) -> int:
    value = os.environ.get(name, str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise SystemExit(f"{name} must be a positive integer")
    return parsed


def _positive_float(name: str, default: float) -> str:
    value = os.environ.get(name, str(default)).strip()
    try:
        parsed = float(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a positive number") from exc
    if not 0 < parsed <= 3600:
        raise SystemExit(f"{name} must be in (0, 3600]")
    return value


def build_command() -> tuple[list[str], Path]:
    """Build the reviewed catalog command from explicit launcher bindings."""

    repo_root = Path(__file__).resolve().parents[1]
    artifact = Path(_required("SBH_ARTIFACT_REF")).expanduser().resolve()
    output_root = Path(_required("SBH_RUN_OUTPUT_DIR")).expanduser()
    if not output_root.is_absolute():
        output_root = (repo_root / output_root).resolve()
    model_id = _required("SBH_MODEL_ID")
    scenario_count = _positive_int("SBH_SCENARIO_COUNT", 1)
    concurrency = _positive_int("SBH_CATALOG_CONCURRENCY", 1)
    timeout = _positive_float("SBH_CATALOG_TIMEOUT", 900.0)
    output = output_root / model_id
    if not artifact.is_dir() or not (artifact / "run_agent.sh").is_file():
        raise SystemExit("SBH_ARTIFACT_REF must contain run_agent.sh")
    command = [
        sys.executable,
        str(repo_root / "tools" / "run_production_catalog.py"),
        "--repo-root",
        str(repo_root),
        "--artifact",
        str(artifact),
        "--artifact-id",
        model_id,
        "--output",
        str(output),
        "--concurrency",
        str(concurrency),
        "--timeout",
        timeout,
    ]
    scenario_id = os.environ.get("SBH_SCENARIO_ID", "").strip()
    ids_file = os.environ.get("SBH_IDS_FILE", "").strip()
    if scenario_id and ids_file:
        raise SystemExit("set only one of SBH_SCENARIO_ID and SBH_IDS_FILE")
    if scenario_id:
        command.extend(["--scenario-id", scenario_id])
    elif ids_file:
        command.extend(["--ids-file", str(Path(ids_file).expanduser().resolve())])
    if scenario_count < 1:
        raise SystemExit("SBH_SCENARIO_COUNT must be positive")
    return command, output


def main() -> int:
    command, output = build_command()
    output.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    source_path = str(repo_root / "src")
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else source_path + os.pathsep + existing_pythonpath
    )
    completed = subprocess.run(
        command,
        cwd=repo_root,
        env=environment,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
