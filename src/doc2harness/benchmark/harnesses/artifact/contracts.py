"""Fail-closed declaration boundary for static artifact scenarios."""

from __future__ import annotations

import re
import unicodedata
import hashlib
from dataclasses import dataclass
from enum import Enum

from doc2harness.benchmark.definition import (
    Classification,
    ComponentKind,
    ComponentRef,
    ExperimentBinding,
    ScenarioDefinition,
    ScenarioDifficulty,
    ScenarioRole,
    UnsupportedPolicy,
    definition_digest,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

_SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z", re.ASCII)
_MAX_PATHS = 8
_MAX_SEGMENTS = 32
_MAX_SEGMENT_BYTES = 255


class ArtifactProbeKind(str, Enum):
    FILE_PRESENT = "FILE_PRESENT"
    FILE_NONEMPTY = "FILE_NONEMPTY"
    DIRECTORY_MEMBER_SUFFIX = "DIRECTORY_MEMBER_SUFFIX"
    DEPENDENCY_DECLARATION = "DEPENDENCY_DECLARATION"


class ArtifactProbeOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class ArtifactProbeApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"


@dataclass(frozen=True, slots=True)
class ArtifactLogicalPath:
    """A bounded path relative to the runtime-owned artifact root."""

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.segments) is not tuple or not self.segments or len(self.segments) > _MAX_SEGMENTS:
            raise ContractDocumentError("artifact path must contain bounded exact segments")
        for segment in self.segments:
            if type(segment) is not str or segment in {"", ".", ".."}:
                raise ContractDocumentError("artifact path contains an invalid segment")
            if unicodedata.normalize("NFC", segment) != segment:
                raise ContractDocumentError("artifact path segment is not Unicode NFC")
            try:
                encoded = segment.encode("utf-8", errors="strict")
            except UnicodeError:
                raise ContractDocumentError("artifact path segment is not valid UTF-8") from None
            if (
                len(encoded) > _MAX_SEGMENT_BYTES
                or "/" in segment
                or "\\" in segment
                or any(ord(character) < 32 or ord(character) == 127 for character in segment)
            ):
                raise ContractDocumentError("artifact path segment is unsafe")


@dataclass(frozen=True, slots=True)
class ArtifactProbe:
    """Declarative probe intent; it contains no host path or execution behavior."""

    kind: ArtifactProbeKind
    paths: tuple[ArtifactLogicalPath, ...]
    member_suffix: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not ArtifactProbeKind:
            raise ContractDocumentError("artifact probe kind must use the closed vocabulary")
        if (
            type(self.paths) is not tuple
            or not self.paths
            or len(self.paths) > _MAX_PATHS
            or any(type(path) is not ArtifactLogicalPath for path in self.paths)
        ):
            raise ContractDocumentError("artifact probe paths must be a bounded exact tuple")
        if len(set(self.paths)) != len(self.paths):
            raise ContractDocumentError("artifact probe paths must be unique")
        if self.kind is ArtifactProbeKind.DIRECTORY_MEMBER_SUFFIX:
            if (
                len(self.paths) != 1
                or type(self.member_suffix) is not str
                or not self.member_suffix.startswith(".")
                or len(self.member_suffix) > 32
                or not self.member_suffix.isascii()
                or any(character in self.member_suffix for character in "/\\")
            ):
                raise ContractDocumentError("directory member probes require one safe suffix")
        elif self.kind is ArtifactProbeKind.DEPENDENCY_DECLARATION:
            expected = (
                ArtifactLogicalPath(("pyproject.toml",)),
                ArtifactLogicalPath(("requirements.txt",)),
            )
            if self.paths != expected or self.member_suffix is not None:
                raise ContractDocumentError("dependency probes require the canonical manifest paths")
        elif len(self.paths) != 1 or self.member_suffix is not None:
            raise ContractDocumentError("file probes require one path and no suffix")


@dataclass(frozen=True, slots=True)
class ArtifactScenario:
    """One immutable scenario definition paired with its declarative probe."""

    definition: ScenarioDefinition
    probe: ArtifactProbe

    def __post_init__(self) -> None:
        if type(self.definition) is not ScenarioDefinition or type(self.probe) is not ArtifactProbe:
            raise ContractDocumentError("artifact scenarios require exact typed values")
        if self.definition.classification.primary != "artifact":
            raise ContractDocumentError("artifact scenario owner differs from its Harness layer")


@dataclass(frozen=True, slots=True)
class ArtifactProbeMetrics:
    """Closed path-free facts emitted by the four static probe shapes."""

    file_exists: bool | None = None
    directory_exists: bool | None = None
    size_bytes: int | None = None
    matching_members: int | None = None
    project_marker: bool | None = None
    requirements_nonempty: bool | None = None

    def __post_init__(self) -> None:
        for value in (
            self.file_exists,
            self.directory_exists,
            self.project_marker,
            self.requirements_nonempty,
        ):
            if value is not None and type(value) is not bool:
                raise ContractDocumentError("artifact metric booleans must be exact bool values")
        for value in (self.size_bytes, self.matching_members):
            if value is not None and (type(value) is not int or value < 0):
                raise ContractDocumentError("artifact metric counts must be non-negative exact integers")


def _digest(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise ContractDocumentError(f"{name} is invalid")
    return value


def _probe_body(probe: ArtifactProbe) -> dict[str, object]:
    return {
        "kind": probe.kind.value,
        "member_suffix": probe.member_suffix,
        "paths": [list(path.segments) for path in probe.paths],
    }


def _metrics_body(metrics: ArtifactProbeMetrics) -> dict[str, object]:
    return {
        "directory_exists": metrics.directory_exists,
        "file_exists": metrics.file_exists,
        "matching_members": metrics.matching_members,
        "project_marker": metrics.project_marker,
        "requirements_nonempty": metrics.requirements_nonempty,
        "size_bytes": metrics.size_bytes,
    }


def _observation_digest(
    scenario_id: str,
    scenario_revision: str,
    semantic_definition_digest: str,
    artifact_id: str,
    artifact_digest: str,
    probe: ArtifactProbe,
    metrics: ArtifactProbeMetrics,
    runtime_evidence_digests: tuple[str, ...],
) -> str:
    body = {
        "artifact": {"artifact_id": artifact_id, "content_digest": artifact_digest},
        "definition_digest": semantic_definition_digest,
        "metrics": _metrics_body(metrics),
        "probe": _probe_body(probe),
        "runtime_evidence_digests": list(runtime_evidence_digests),
        "scenario_id": scenario_id,
        "scenario_revision": scenario_revision,
    }
    domain = b"doc2harness.artifact-static-probe-evidence/1\0"
    return "sha256:" + hashlib.sha256(domain + canonical_json(body)).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactProbeObservation:
    """Path-free facts bound to definition, artifact, probe and runtime evidence."""

    scenario_id: str
    scenario_revision: str
    definition_digest: str
    artifact_id: str
    artifact_digest: str
    probe: ArtifactProbe
    metrics: ArtifactProbeMetrics
    runtime_evidence_digests: tuple[str, ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _SCENARIO_ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("artifact observation scenario id is invalid")
        if type(self.scenario_revision) is not str or not self.scenario_revision.isascii():
            raise ContractDocumentError("artifact observation revision is invalid")
        _digest(self.definition_digest, "artifact observation definition digest")
        if type(self.artifact_id) is not str or _SCENARIO_ID.fullmatch(self.artifact_id) is None:
            raise ContractDocumentError("artifact observation artifact id is invalid")
        _digest(self.artifact_digest, "artifact observation artifact digest")
        if type(self.probe) is not ArtifactProbe or type(self.metrics) is not ArtifactProbeMetrics:
            raise ContractDocumentError("artifact observation uses invalid typed facts")
        if type(self.runtime_evidence_digests) is not tuple or not self.runtime_evidence_digests:
            raise ContractDocumentError("artifact observation requires runtime evidence")
        for value in self.runtime_evidence_digests:
            _digest(value, "artifact runtime evidence digest")
        _digest(self.evidence_digest, "artifact observation digest")
        expected = _observation_digest(
            self.scenario_id,
            self.scenario_revision,
            self.definition_digest,
            self.artifact_id,
            self.artifact_digest,
            self.probe,
            self.metrics,
            self.runtime_evidence_digests,
        )
        if self.evidence_digest != expected:
            raise ContractDocumentError("artifact observation evidence does not bind its facts")


def create_artifact_probe_observation(
    scenario: ArtifactScenario,
    artifact_id: str,
    artifact_digest: str,
    metrics: ArtifactProbeMetrics,
    runtime_evidence_digests: tuple[str, ...],
    /,
) -> ArtifactProbeObservation:
    """Seal runtime facts to their scenario, artifact and lower-level evidence."""

    if type(scenario) is not ArtifactScenario or type(metrics) is not ArtifactProbeMetrics:
        raise ContractDocumentError("artifact observation inputs use invalid typed contracts")
    semantic_digest = definition_digest(scenario.definition)
    evidence = _observation_digest(
        scenario.definition.scenario_id,
        scenario.definition.revision,
        semantic_digest,
        artifact_id,
        artifact_digest,
        scenario.probe,
        metrics,
        runtime_evidence_digests,
    )
    return ArtifactProbeObservation(
        scenario.definition.scenario_id,
        scenario.definition.revision,
        semantic_digest,
        artifact_id,
        artifact_digest,
        scenario.probe,
        metrics,
        runtime_evidence_digests,
        evidence,
    )


@dataclass(frozen=True, slots=True)
class ArtifactProbeEvaluation:
    """Applicable PASS/FAIL projection for one static artifact target."""

    scenario_id: str
    outcome: ArtifactProbeOutcome
    applicability: ArtifactProbeApplicability
    reason: str
    evidence_digest: str
    contribution_microunits: int

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _SCENARIO_ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("artifact evaluation scenario id is invalid")
        if type(self.outcome) is not ArtifactProbeOutcome:
            raise ContractDocumentError("artifact evaluation outcome is invalid")
        if self.applicability is not ArtifactProbeApplicability.APPLICABLE:
            raise ContractDocumentError("artifact evaluation must be applicable")
        if type(self.reason) is not str or not self.reason:
            raise ContractDocumentError("artifact evaluation reason is invalid")
        _digest(self.evidence_digest, "artifact evaluation evidence digest")
        if type(self.contribution_microunits) is not int or self.contribution_microunits < 0:
            raise ContractDocumentError("artifact evaluation contribution is invalid")


def define_artifact_scenario(scenario_id: str, probe: ArtifactProbe, /) -> ArtifactScenario:
    """Construct a target definition owned by the static artifact Harness layer."""

    if type(scenario_id) is not str or _SCENARIO_ID.fullmatch(scenario_id) is None:
        raise ContractDocumentError("artifact scenario id is not canonical")
    if type(probe) is not ArtifactProbe:
        raise ContractDocumentError("artifact scenario probe must use ArtifactProbe")
    definition = ScenarioDefinition(
        scenario_id=scenario_id,
        revision="1",
        classification=Classification("artifact"),
        role=ScenarioRole.TARGET,
        weight_microunits=1_000_000,
        difficulty=ScenarioDifficulty.STANDARD,
        dependencies=(),
        experiment=ExperimentBinding(
            fixture=ComponentRef("artifact-readonly-workspace", "1", ComponentKind.FIXTURE),
            stimulus=ComponentRef.no_op(),
            observer=ComponentRef("artifact-static-observer", "1", ComponentKind.OBSERVER),
            oracle=ComponentRef("artifact-static-oracle", "1", ComponentKind.ORACLE),
            cleanup=ComponentRef.no_op(),
        ),
        unsupported_policy=UnsupportedPolicy.FAIL,
    )
    return ArtifactScenario(definition, probe)


def validate_artifact_observation(
    scenario: ArtifactScenario,
    observation: ArtifactProbeObservation,
    /,
) -> None:
    """Validate the shared identity binding before a local scenario judge runs."""

    if type(scenario) is not ArtifactScenario:
        raise ContractDocumentError("artifact evaluation requires an exact scenario contract")
    if type(observation) is not ArtifactProbeObservation:
        raise ContractDocumentError("artifact observation uses an invalid typed contract")
    definition = scenario.definition
    if (
        observation.scenario_id != definition.scenario_id
        or observation.scenario_revision != definition.revision
        or observation.definition_digest != definition_digest(definition)
        or observation.probe != scenario.probe
    ):
        raise ContractDocumentError("artifact observation does not bind the scenario definition")


def build_artifact_evaluation(
    scenario: ArtifactScenario,
    observation: ArtifactProbeObservation,
    satisfied: bool,
    reason: str,
    /,
) -> ArtifactProbeEvaluation:
    """Build the frozen artifact PASS/FAIL projection after local judgment."""

    validate_artifact_observation(scenario, observation)
    return ArtifactProbeEvaluation(
        scenario.definition.scenario_id,
        ArtifactProbeOutcome.PASS if satisfied else ArtifactProbeOutcome.FAIL,
        ArtifactProbeApplicability.APPLICABLE,
        reason,
        observation.evidence_digest,
        scenario.definition.weight_microunits if satisfied else 0,
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
    "build_artifact_evaluation",
    "define_artifact_scenario",
    "validate_artifact_observation",
]
