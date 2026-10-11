"""Execute an explicit catalog selection through the active production boundary.

The command is separate from the YAML launcher.  It accepts only a reviewed artifact
path, a scenario selection, and an explicitly named deterministic stimulus
root; every observation still passes through ``runtime.catalog_executor``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from doc2harness.benchmark.dispatch import active_scenario_ids, load_scenario_owner
from doc2harness.runtime.artifact_executor import artifact_tree_digest
from doc2harness.runtime.catalog_executor import CatalogExecutionRequest, run_catalog


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _selection(
    repo_root: Path,
    ids_file: Path | None,
    scenario_id: str | None,
    family: str | None,
) -> tuple[str, ...]:
    selected = sum(value is not None for value in (ids_file, scenario_id, family))
    if selected > 1:
        raise ValueError("use only one of --ids-file, --scenario-id, or --family")
    if scenario_id:
        return (scenario_id,)
    if family:
        values = tuple(
            scenario_id
            for scenario_id in active_scenario_ids()
            if load_scenario_owner(scenario_id).definition.classification.primary == family
        )
        if not values:
            raise ValueError(f"active catalog contains no family: {family}")
        return values
    if ids_file is not None:
        values = tuple(
            line.strip() for line in ids_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if not values or len(values) != len(set(values)):
            raise ValueError("scenario ids file must contain unique non-empty IDs")
        return values
    return active_scenario_ids()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids-file", type=Path)
    parser.add_argument("--scenario-id")
    parser.add_argument("--family", help="select every active scenario with this primary classification")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    artifact = args.artifact.resolve()
    selection = _selection(
        repo_root,
        args.ids_file.resolve() if args.ids_file else None,
        args.scenario_id,
        args.family,
    )
    source_digest = _digest("sbh-production-driver-source/1")
    task_digest = _digest("sbh-production-catalog-task/1:" + ",".join(selection))
    budget_digest = _digest(f"sbh-production-catalog-budget/1:{args.timeout}:{args.concurrency}")
    # The universal dispatcher selects a reviewed family driver from the
    # catalog owner.  Reviewed routes cover direct, permission,
    # file, artifact, state, headless, startup, TUI,
    # and extension counterfactuals.  Unsupported families fail closed per
    # scenario rather than receiving a direct-mock execution by accident.
    #
    # It is invoked as an installed module rather than a repository file so an
    # installed wheel can execute a test point without a checkout on disk.
    request = CatalogExecutionRequest(
        artifact=artifact,
        driver_root=repo_root,
        driver_argv=(
            sys.executable,
            "-m",
            "doc2harness.runtime.driver_dispatch",
        ),
        output_dir=args.output.resolve(),
        artifact_id=args.artifact_id,
        source_digest=source_digest,
        task_digest=task_digest,
        budget_digest=budget_digest,
        artifact_digest=artifact_tree_digest(artifact),
        scenario_ids=selection,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout,
    )
    os.environ.setdefault("PYTHONPATH", str(repo_root / "src"))
    receipt = run_catalog(
        request,
        environ={
            "PYTHONPATH": str(repo_root / "src"),
            "SBH_EVALUATION_IMAGE": os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate"),
        },
    )
    print(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
