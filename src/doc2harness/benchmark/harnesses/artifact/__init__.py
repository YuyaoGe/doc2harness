"""Typed declarations for the static artifact Harness layer."""

from doc2harness.benchmark.harnesses.artifact.contracts import (
    ArtifactLogicalPath,
    ArtifactProbe,
    ArtifactProbeApplicability,
    ArtifactProbeEvaluation,
    ArtifactProbeKind,
    ArtifactProbeMetrics,
    ArtifactProbeObservation,
    ArtifactProbeOutcome,
    ArtifactScenario,
    create_artifact_probe_observation,
    define_artifact_scenario,
)

__all__ = [
    "ArtifactLogicalPath",
    "ArtifactProbe",
    "ArtifactProbeApplicability",
    "ArtifactProbeEvaluation",
    "ArtifactProbeKind",
    "ArtifactProbeMetrics",
    "ArtifactProbeObservation",
    "ArtifactProbeOutcome",
    "ArtifactScenario",
    "create_artifact_probe_observation",
    "define_artifact_scenario",
]
