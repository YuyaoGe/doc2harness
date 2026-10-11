"""Immutable scenario definitions without execution behavior."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z", re.ASCII)
_REVISION = re.compile(r"[1-9][0-9]{0,8}\Z", re.ASCII)
_DIGEST_DOMAIN = b"doc2harness.scenario-definition/1\x00"
_MAX_CLASSIFICATIONS = 32
_MAX_DEPENDENCIES = 256
_MAX_WEIGHT = 2**63 - 1


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ContractDocumentError(f"{name} is not a canonical identifier")
    return value


class ScenarioRole(str, Enum):
    TARGET = "TARGET"
    CONTROL = "CONTROL"


class ScenarioDifficulty(str, Enum):
    STANDARD = "STANDARD"
    HARD = "HARD"


class UnsupportedPolicy(str, Enum):
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ComponentKind(str, Enum):
    FIXTURE = "FIXTURE"
    STIMULUS = "STIMULUS"
    OBSERVER = "OBSERVER"
    ORACLE = "ORACLE"
    CLEANUP = "CLEANUP"
    NO_OP = "NO_OP"


class DependencyKind(str, Enum):
    REQUIREMENT = "REQUIREMENT"
    CONTROL = "CONTROL"
    GATE = "GATE"


@dataclass(frozen=True, slots=True)
class Classification:
    """One stable primary function and zero or more ordered secondary functions."""

    primary: str
    secondary: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.primary, "primary classification")
        if type(self.secondary) is not tuple:
            raise ContractDocumentError("secondary classifications must be an exact tuple")
        if len(self.secondary) > _MAX_CLASSIFICATIONS:
            raise ContractDocumentError("secondary classifications exceed the size limit")
        for item in self.secondary:
            _identifier(item, "secondary classification")
        if self.primary in self.secondary or len(set(self.secondary)) != len(self.secondary):
            raise ContractDocumentError("classifications must be unique")
        if self.secondary != tuple(sorted(self.secondary)):
            raise ContractDocumentError("secondary classifications must be sorted")


@dataclass(frozen=True, slots=True)
class ComponentRef:
    """A versioned component identity; NO_OP is explicit, never represented by None."""

    component_id: str
    revision: str
    kind: ComponentKind

    def __post_init__(self) -> None:
        _identifier(self.component_id, "component id")
        if type(self.revision) is not str or _REVISION.fullmatch(self.revision) is None:
            raise ContractDocumentError("component revision is not canonical")
        if type(self.kind) is not ComponentKind:
            raise ContractDocumentError("component kind must use ComponentKind")
        if self.kind is ComponentKind.NO_OP and self.component_id != "no-op":
            raise ContractDocumentError("NO_OP must use the canonical no-op component")
        if self.kind is not ComponentKind.NO_OP and self.component_id == "no-op":
            raise ContractDocumentError("the canonical no-op component must use NO_OP")

    @classmethod
    def no_op(cls) -> ComponentRef:
        return cls("no-op", "1", ComponentKind.NO_OP)


@dataclass(frozen=True, slots=True)
class DependencyRef:
    scenario_id: str
    kind: DependencyKind

    def __post_init__(self) -> None:
        _identifier(self.scenario_id, "dependency scenario id")
        if type(self.kind) is not DependencyKind:
            raise ContractDocumentError("dependency kind must use DependencyKind")


@dataclass(frozen=True, slots=True)
class ExperimentBinding:
    fixture: ComponentRef
    stimulus: ComponentRef
    observer: ComponentRef
    oracle: ComponentRef
    cleanup: ComponentRef

    def __post_init__(self) -> None:
        expected = (
            (self.fixture, ComponentKind.FIXTURE),
            (self.stimulus, ComponentKind.STIMULUS),
            (self.observer, ComponentKind.OBSERVER),
            (self.oracle, ComponentKind.ORACLE),
            (self.cleanup, ComponentKind.CLEANUP),
        )
        for value, kind in expected:
            if type(value) is not ComponentRef:
                raise ContractDocumentError("experiment bindings require exact ComponentRef values")
            if value.kind not in {kind, ComponentKind.NO_OP}:
                raise ContractDocumentError("component kind does not match its binding slot")


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    revision: str
    classification: Classification
    role: ScenarioRole
    weight_microunits: int
    difficulty: ScenarioDifficulty
    dependencies: tuple[DependencyRef, ...]
    experiment: ExperimentBinding
    unsupported_policy: UnsupportedPolicy

    def __post_init__(self) -> None:
        _identifier(self.scenario_id, "scenario id")
        if type(self.revision) is not str or _REVISION.fullmatch(self.revision) is None:
            raise ContractDocumentError("scenario revision is not canonical")
        if type(self.classification) is not Classification:
            raise ContractDocumentError("classification must use the exact typed structure")
        if type(self.role) is not ScenarioRole:
            raise ContractDocumentError("role must use ScenarioRole")
        if (
            type(self.weight_microunits) is not int
            or self.weight_microunits < 0
            or self.weight_microunits > _MAX_WEIGHT
        ):
            raise ContractDocumentError("weight must be a bounded non-negative exact integer")
        if (self.role is ScenarioRole.TARGET) != (self.weight_microunits > 0):
            raise ContractDocumentError("targets require positive weight and controls require zero weight")
        if type(self.difficulty) is not ScenarioDifficulty:
            raise ContractDocumentError("difficulty must use ScenarioDifficulty")
        if type(self.dependencies) is not tuple or len(self.dependencies) > _MAX_DEPENDENCIES:
            raise ContractDocumentError("dependencies must be a bounded exact tuple")
        if any(type(item) is not DependencyRef for item in self.dependencies):
            raise ContractDocumentError("dependencies must be an exact tuple of DependencyRef")
        identities = tuple((item.scenario_id, item.kind.value) for item in self.dependencies)
        if len(set(identities)) != len(identities) or identities != tuple(sorted(identities)):
            raise ContractDocumentError("dependencies must be sorted and unique")
        if any(item.scenario_id == self.scenario_id for item in self.dependencies):
            raise ContractDocumentError("scenario cannot depend on itself")
        if type(self.experiment) is not ExperimentBinding:
            raise ContractDocumentError("experiment must use ExperimentBinding")
        if type(self.unsupported_policy) is not UnsupportedPolicy:
            raise ContractDocumentError("unsupported policy must use UnsupportedPolicy")


def _component_body(value: ComponentRef) -> dict[str, str]:
    return {"component_id": value.component_id, "kind": value.kind.value, "revision": value.revision}


def definition_digest(definition: ScenarioDefinition, /) -> str:
    """Return the domain-separated semantic digest of one typed definition."""

    if type(definition) is not ScenarioDefinition:
        raise ContractDocumentError("definition must use ScenarioDefinition")
    body = {
        "classification": {
            "primary": definition.classification.primary,
            "secondary": list(definition.classification.secondary),
        },
        "dependencies": [
            {"kind": item.kind.value, "scenario_id": item.scenario_id}
            for item in definition.dependencies
        ],
        "difficulty": definition.difficulty.value,
        "experiment": {
            "cleanup": _component_body(definition.experiment.cleanup),
            "fixture": _component_body(definition.experiment.fixture),
            "observer": _component_body(definition.experiment.observer),
            "oracle": _component_body(definition.experiment.oracle),
            "stimulus": _component_body(definition.experiment.stimulus),
        },
        "revision": definition.revision,
        "role": definition.role.value,
        "scenario_id": definition.scenario_id,
        "unsupported_policy": definition.unsupported_policy.value,
        "weight_microunits": definition.weight_microunits,
    }
    return "sha256:" + hashlib.sha256(_DIGEST_DOMAIN + canonical_json(body)).hexdigest()


__all__ = [
    "Classification", "ComponentKind", "ComponentRef", "DependencyKind",
    "DependencyRef", "ExperimentBinding", "ScenarioDefinition", "ScenarioDifficulty",
    "ScenarioRole", "UnsupportedPolicy", "definition_digest",
]
