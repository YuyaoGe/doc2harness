"""Run one ``artifact`` probe against the private artifact copy."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from doc2harness.runtime.driver_evidence import evidence_digest as _digest


def _run_one() -> dict[str, object]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.artifact.contracts import (
        ArtifactProbeKind,
        ArtifactProbeMetrics,
        create_artifact_probe_observation,
    )

    scenario_id = os.environ["SBH_SCENARIO_ID"]
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.artifact." not in owner.module:
        raise RuntimeError("artifact driver received a non-artifact scenario")
    scenario = owner.scenario
    root = Path(os.environ["SBH_ARTIFACT_PATH"])
    probe = scenario.probe
    relative = "/".join(probe.paths[0].segments)
    target = root / relative
    file_exists = target.is_file()
    directory_exists = target.is_dir()
    size_bytes = target.stat().st_size if file_exists and probe.kind is ArtifactProbeKind.FILE_NONEMPTY else None
    matching_members = None
    project_marker = None
    requirements_nonempty = None
    if probe.kind is ArtifactProbeKind.DIRECTORY_MEMBER_SUFFIX:
        matching_members = sum(1 for child in target.iterdir() if child.is_file() and child.name.endswith(probe.member_suffix or "")) if directory_exists else 0
    elif probe.kind is ArtifactProbeKind.DEPENDENCY_DECLARATION:
        pyproject = root / "pyproject.toml"
        requirements = root / "requirements.txt"
        project_marker = pyproject.is_file() and "[project" in pyproject.read_text(encoding="utf-8", errors="replace")
        requirements_nonempty = requirements.is_file() and requirements.stat().st_size > 0
    metrics = ArtifactProbeMetrics(
        file_exists=file_exists if probe.kind in {ArtifactProbeKind.FILE_PRESENT, ArtifactProbeKind.FILE_NONEMPTY} else None,
        directory_exists=directory_exists if probe.kind is ArtifactProbeKind.DIRECTORY_MEMBER_SUFFIX else None,
        size_bytes=size_bytes,
        matching_members=matching_members,
        project_marker=project_marker,
        requirements_nonempty=requirements_nonempty,
    )
    evidence = (_digest({"path": relative, "exists": file_exists, "directory": directory_exists, "size": size_bytes, "members": matching_members, "project": project_marker, "requirements": requirements_nonempty}),)
    observation = create_artifact_probe_observation(
        scenario,
        os.environ["SBH_ARTIFACT_ID"],
        os.environ["SBH_ARTIFACT_DIGEST"],
        metrics,
        evidence,
    )
    return {"observation": observation, "judgement": owner.judge(observation), "module": owner.module,
            "definition_digest": definition_digest(owner.definition)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = _run_one()
    target = Path(os.environ["SBH_TYPED_EVENT_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "schema": "doc2harness.typed-events/1",
        "source_digest": os.environ["SBH_SOURCE_DIGEST"], "task_digest": os.environ["SBH_TASK_DIGEST"],
        "budget_digest": os.environ["SBH_BUDGET_DIGEST"], "artifact_digest": os.environ["SBH_ARTIFACT_DIGEST"],
        "artifact_id": os.environ["SBH_ARTIFACT_ID"], "scenario_id": os.environ["SBH_SCENARIO_ID"],
        "scenario_module": result["module"], "definition_digest": result["definition_digest"],
        "events": [{"type": "artifact-observation", "observation": asdict(result["observation"]),
                    "judgement": asdict(result["judgement"])}],
    }, ensure_ascii=False, default=lambda value: value.value if hasattr(value, "value") else str(value), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
