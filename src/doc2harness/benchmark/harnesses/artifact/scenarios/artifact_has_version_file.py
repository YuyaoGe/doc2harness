"""Verify the artifact has a non-empty version marker.

1. Probe the version-file path.
2. Capture presence and non-empty-content facts.
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
    "artifact_has_version_file",
    ArtifactProbe(ArtifactProbeKind.FILE_NONEMPTY, (ArtifactLogicalPath(("VERSION",)),)),
)
definition = scenario.definition
SCENARIO = scenario
STEPS = (scenario.probe,)


def judge(observation: ArtifactProbeObservation):
    validate_artifact_observation(SCENARIO, observation)
    metrics = observation.metrics
    if metrics.file_exists is None or any(
        value is not None
        for value in (
            metrics.directory_exists,
            metrics.matching_members,
            metrics.project_marker,
            metrics.requirements_nonempty,
        )
    ):
        raise ContractDocumentError("non-empty file observation is incomplete")
    if metrics.file_exists and metrics.size_bytes is None:
        raise ContractDocumentError("non-empty file observation lacks its size")
    satisfied = metrics.file_exists and (metrics.size_bytes or 0) > 0
    if metrics.file_exists:
        reason = f"VERSION present but size={metrics.size_bytes}B (must be non-empty) (v6§0.1.1:20 the artifact root must contain `VERSION`)"
    else:
        reason = "VERSION present=False (v6§0.1.1:20 the artifact root must contain `VERSION`)"
    return build_artifact_evaluation(SCENARIO, observation, satisfied, reason)

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
