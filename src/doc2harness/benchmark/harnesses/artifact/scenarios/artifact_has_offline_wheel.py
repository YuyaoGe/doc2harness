"""Verify the artifact includes an offline wheel.

1. Probe the expected wheel path.
2. Capture its presence and file-kind facts.
3. Apply the typed artifact oracle to the observation.
"""

from doc2harness.benchmark.harnesses.artifact.contracts import (
    ArtifactLogicalPath,
    ArtifactProbe,
    ArtifactProbeKind,
    ArtifactProbeObservation,
    build_artifact_evaluation,
    define_artifact_scenario,
    validate_artifact_observation,
)
from doc2harness.contracts.primitives import ContractDocumentError

scenario = define_artifact_scenario(
    "artifact_has_offline_wheel",
    ArtifactProbe(
        ArtifactProbeKind.DIRECTORY_MEMBER_SUFFIX,
        (ArtifactLogicalPath(("wheels",)),),
        ".whl",
    ),
)
definition = scenario.definition
SCENARIO = scenario
STEPS = (scenario.probe,)


def judge(observation: ArtifactProbeObservation):
    validate_artifact_observation(SCENARIO, observation)
    metrics = observation.metrics
    if metrics.directory_exists is None or metrics.matching_members is None or any(
        value is not None
        for value in (
            metrics.file_exists,
            metrics.size_bytes,
            metrics.project_marker,
            metrics.requirements_nonempty,
        )
    ):
        raise ContractDocumentError("directory member observation is incomplete")
    satisfied = metrics.directory_exists and metrics.matching_members > 0
    reason = (
        f"wheels directory present={metrics.directory_exists} containing *.whl x {metrics.matching_members} ("
        "v6§0.1.1:20 `wheels/` must hold at least one offline wheel)"
    )
    return build_artifact_evaluation(SCENARIO, observation, satisfied, reason)

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
