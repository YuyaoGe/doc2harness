"""Verify the artifact contains its run-agent launcher.

1. Probe the launcher path.
2. Capture its presence and executable-file facts.
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
    "artifact_has_run_agent_sh",
    ArtifactProbe(ArtifactProbeKind.FILE_PRESENT, (ArtifactLogicalPath(("run_agent.sh",)),)),
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
            metrics.size_bytes,
            metrics.matching_members,
            metrics.project_marker,
            metrics.requirements_nonempty,
        )
    ):
        raise ContractDocumentError("file presence observation is incomplete")
    return build_artifact_evaluation(
        SCENARIO,
        observation,
        metrics.file_exists,
        f"run_agent.sh present={metrics.file_exists} (v6§0.1.1:20 the artifact root must contain `run_agent.sh`)",
    )

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
