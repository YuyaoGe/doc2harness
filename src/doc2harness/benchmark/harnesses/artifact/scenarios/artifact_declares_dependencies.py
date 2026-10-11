"""Verify the artifact declares its runtime dependencies.

1. Inspect the declared dependency metadata.
2. Capture the expected logical artifact path.
3. Apply the typed artifact oracle and require a valid declaration.
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
    "artifact_declares_dependencies",
    ArtifactProbe(
        ArtifactProbeKind.DEPENDENCY_DECLARATION,
        (
            ArtifactLogicalPath(("pyproject.toml",)),
            ArtifactLogicalPath(("requirements.txt",)),
        ),
    ),
)
definition = scenario.definition
SCENARIO = scenario
STEPS = (scenario.probe,)


def judge(observation: ArtifactProbeObservation):
    validate_artifact_observation(SCENARIO, observation)
    metrics = observation.metrics
    if metrics.project_marker is None or metrics.requirements_nonempty is None or any(
        value is not None
        for value in (
            metrics.file_exists,
            metrics.directory_exists,
            metrics.size_bytes,
            metrics.matching_members,
        )
    ):
        raise ContractDocumentError("dependency declaration observation is incomplete")
    satisfied = metrics.project_marker or metrics.requirements_nonempty
    reason = (
        f"pyproject.toml has [project...]={metrics.project_marker} requirements.txt non-empty="
        f"{metrics.requirements_nonempty} (either one suffices) (v6§0.1.1:25 a new third-party dependency "
        "must be declared in `[project.dependencies]` of `pyproject.toml` (or in `requirements.txt`))"
    )
    return build_artifact_evaluation(SCENARIO, observation, satisfied, reason)

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
