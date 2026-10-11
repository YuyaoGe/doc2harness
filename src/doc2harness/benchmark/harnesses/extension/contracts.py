"""Closed declarations and observations for extension counterfactuals."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from doc2harness.benchmark.definition import (
    Classification,
    ComponentKind,
    ComponentRef,
    DependencyKind,
    DependencyRef,
    ExperimentBinding,
    ScenarioDefinition,
    ScenarioDifficulty,
    ScenarioRole,
    UnsupportedPolicy,
    definition_digest,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z", re.ASCII)


class ExtensionFace(str, Enum):
    COMMANDS = "commands"
    SKILLS = "skills"
    HOOKS = "hooks"
    PLUGINS = "plugins"


class ExtensionOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class ExtensionApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class ExtensionFixtureStep:
    """Declare which workspace views the shared fixture must materialize."""

    modes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.modes not in {("inside",), ("inside", "outside")}:
            raise ContractDocumentError("extension fixture step modes are invalid")


@dataclass(frozen=True, slots=True)
class ExtensionSetupStep:
    """Declare one setup command sent before the probe round."""

    command: str

    def __post_init__(self) -> None:
        if type(self.command) is not str or not self.command:
            raise ContractDocumentError("extension setup step command is invalid")


@dataclass(frozen=True, slots=True)
class ExtensionToolRoundStep:
    """Declare the tool and command used for the extension probe."""

    tool: str
    command: str

    def __post_init__(self) -> None:
        if self.tool != "bash" or type(self.command) is not str or not self.command:
            raise ContractDocumentError("extension tool round step is invalid")


@dataclass(frozen=True, slots=True)
class ExtensionCaptureStep:
    """Declare which fixture view contributes the extension-face observation."""

    mode: str
    faces: tuple[ExtensionFace, ...]

    def __post_init__(self) -> None:
        if self.mode not in {"inside", "outside"}:
            raise ContractDocumentError("extension capture step mode is invalid")
        if (
            type(self.faces) is not tuple
            or any(type(face) is not ExtensionFace for face in self.faces)
            or len(set(self.faces)) != len(self.faces)
        ):
            raise ContractDocumentError("extension capture step faces are invalid")


def materialize_symlink_fixture(
    *, modes: tuple[str, ...]
) -> ExtensionFixtureStep:
    """Declare the inside/outside symlink views for the shared fixture."""

    return ExtensionFixtureStep(modes)


def setup_command(command: str) -> ExtensionSetupStep:
    """Declare one extension setup command."""

    return ExtensionSetupStep(command)


def tool_round(*, tool: str, command: str) -> ExtensionToolRoundStep:
    """Declare the tool round used to probe extension discovery."""

    return ExtensionToolRoundStep(tool, command)


def capture_extension_face(
    *, mode: str, faces: tuple[ExtensionFace, ...]
) -> ExtensionCaptureStep:
    """Declare typed extension faces observed in one fixture view."""

    return ExtensionCaptureStep(mode, faces)


ExtensionScenarioStep = (
    ExtensionFixtureStep
    | ExtensionSetupStep
    | ExtensionToolRoundStep
    | ExtensionCaptureStep
)


@dataclass(frozen=True, slots=True)
class ExtensionCounterfactualRecipe:
    setup_commands: tuple[str, ...]
    round_tool: str
    round_command: str
    paired: bool
    dependencies: tuple[tuple[str, DependencyKind], ...] = ()
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        if (
            type(self.setup_commands) is not tuple
            or not self.setup_commands
            or any(type(value) is not str or not value for value in self.setup_commands)
        ):
            raise ContractDocumentError("extension setup commands are invalid")
        if self.round_tool != "bash" or type(self.round_command) is not str or not self.round_command:
            raise ContractDocumentError("extension gateway round is invalid")
        if type(self.paired) is not bool:
            raise ContractDocumentError("extension counterfactual mode is invalid")
        if type(self.dependencies) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not DependencyKind
            for item in self.dependencies
        ):
            raise ContractDocumentError("extension dependencies are invalid")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ContractDocumentError("extension timeout is invalid")


def compile_extension_steps(
    steps: tuple[ExtensionScenarioStep, ...],
    /,
    *,
    dependencies: tuple[tuple[str, DependencyKind], ...] = (),
    timeout_seconds: int = 60,
) -> ExtensionCounterfactualRecipe:
    """Compile visible extension steps into the runtime recipe."""

    if type(steps) is not tuple or not steps or type(steps[0]) is not ExtensionFixtureStep:
        raise ContractDocumentError("extension steps must start with one fixture step")
    fixture = steps[0]
    cursor = 1
    setup_commands: list[str] = []
    while cursor < len(steps) and type(steps[cursor]) is ExtensionSetupStep:
        setup_commands.append(steps[cursor].command)
        cursor += 1
    if not setup_commands or cursor >= len(steps) or type(steps[cursor]) is not ExtensionToolRoundStep:
        raise ContractDocumentError("extension steps require setup commands and one tool round")
    tool_round = steps[cursor]
    cursor += 1
    captures = steps[cursor:]
    if len(captures) != len(fixture.modes) or any(
        type(step) is not ExtensionCaptureStep for step in captures
    ):
        raise ContractDocumentError("extension capture steps do not match fixture modes")
    if tuple(step.mode for step in captures) != fixture.modes:
        raise ContractDocumentError("extension capture steps must follow fixture modes")
    return ExtensionCounterfactualRecipe(
        setup_commands=tuple(setup_commands),
        round_tool=tool_round.tool,
        round_command=tool_round.command,
        paired=fixture.modes == ("inside", "outside"),
        dependencies=dependencies,
        timeout_seconds=timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class ExtensionCounterfactualScenario:
    definition: ScenarioDefinition
    recipe: ExtensionCounterfactualRecipe

    def __post_init__(self) -> None:
        if type(self.definition) is not ScenarioDefinition or type(self.recipe) is not ExtensionCounterfactualRecipe:
            raise ContractDocumentError("extension scenario requires exact typed values")
        if self.definition.classification.primary != "extension":
            raise ContractDocumentError("extension scenario owner differs from its Harness")


@dataclass(frozen=True, slots=True)
class ExtensionRunObservation:
    fixture_verified: bool
    live: bool
    request_count: int
    seen_faces: tuple[ExtensionFace, ...]
    fixture_error_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.fixture_verified) is not bool or type(self.live) is not bool:
            raise ContractDocumentError("extension run booleans are invalid")
        if type(self.request_count) is not int or self.request_count < 0:
            raise ContractDocumentError("extension request count is invalid")
        if (
            type(self.seen_faces) is not tuple
            or any(type(face) is not ExtensionFace for face in self.seen_faces)
            or len(set(self.seen_faces)) != len(self.seen_faces)
        ):
            raise ContractDocumentError("extension seen faces are invalid")
        if self.fixture_error_code is not None and (
            type(self.fixture_error_code) is not str
            or not self.fixture_error_code
            or len(self.fixture_error_code) > 128
        ):
            raise ContractDocumentError("extension fixture error is invalid")


@dataclass(frozen=True, slots=True)
class ExtensionCounterfactualObservation:
    scenario_id: str
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    backend_evidence_digest: str
    inside: ExtensionRunObservation
    outside: ExtensionRunObservation | None
    process_tree_clean: bool
    workspace_clean: bool

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("extension observation id is invalid")
        for name, value in (
            ("plan", self.plan_digest),
            ("artifact", self.artifact_digest),
            ("backend evidence", self.backend_evidence_digest),
        ):
            if type(value) is not str or not value.startswith("sha256:") or len(value) != 71:
                raise ContractDocumentError(f"extension {name} digest is invalid")
        if type(self.artifact_id) is not str or not self.artifact_id:
            raise ContractDocumentError("extension artifact id is invalid")
        if type(self.inside) is not ExtensionRunObservation or (
            self.outside is not None and type(self.outside) is not ExtensionRunObservation
        ):
            raise ContractDocumentError("extension run observation is invalid")
        if type(self.process_tree_clean) is not bool or type(self.workspace_clean) is not bool:
            raise ContractDocumentError("extension cleanup facts are invalid")


@dataclass(frozen=True, slots=True)
class ExtensionCounterfactualEvaluation:
    scenario_id: str
    outcome: ExtensionOutcome
    applicability: ExtensionApplicability
    reason_code: str
    evidence_digest: str
    contribution_microunits: int

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("extension evaluation id is invalid")
        if type(self.outcome) is not ExtensionOutcome or type(self.applicability) is not ExtensionApplicability:
            raise ContractDocumentError("extension evaluation closed value is invalid")
        if type(self.reason_code) is not str or not self.reason_code or len(self.reason_code) > 128:
            raise ContractDocumentError("extension evaluation reason is invalid")
        if type(self.evidence_digest) is not str or not self.evidence_digest.startswith("sha256:") or len(self.evidence_digest) != 71:
            raise ContractDocumentError("extension evaluation digest is invalid")
        if self.contribution_microunits not in {0, 1_000_000}:
            raise ContractDocumentError("extension evaluation contribution is invalid")


@runtime_checkable
class ExtensionCounterfactualObserver(Protocol):
    def observe(self, scenario: ExtensionCounterfactualScenario, /) -> ExtensionCounterfactualObservation: ...


def define_extension_scenario(
    scenario_id: str,
    recipe: ExtensionCounterfactualRecipe,
    role: ScenarioRole,
    /,
) -> ExtensionCounterfactualScenario:
    if type(scenario_id) is not str or _ID.fullmatch(scenario_id) is None:
        raise ContractDocumentError("extension scenario id is invalid")
    dependencies = tuple(
        sorted(
            (DependencyRef(test_id, kind) for test_id, kind in recipe.dependencies),
            key=lambda item: (item.scenario_id, item.kind.value),
        )
    )
    definition = ScenarioDefinition(
        scenario_id,
        "1",
        Classification("extension"),
        role,
        1_000_000 if role is ScenarioRole.TARGET else 0,
        ScenarioDifficulty.HARD,
        dependencies,
        ExperimentBinding(
            ComponentRef("extension-counterfactual-fixture", "1", ComponentKind.FIXTURE),
            ComponentRef("extension-counterfactual-adapter", "1", ComponentKind.STIMULUS),
            ComponentRef("extension-counterfactual-observer", "1", ComponentKind.OBSERVER),
            ComponentRef("extension-counterfactual-oracle", "1", ComponentKind.ORACLE),
            ComponentRef("extension-counterfactual-cleanup", "1", ComponentKind.CLEANUP),
        ),
        UnsupportedPolicy.FAIL,
    )
    return ExtensionCounterfactualScenario(definition, recipe)


def evaluation_digest(
    scenario: ExtensionCounterfactualScenario,
    observation: ExtensionCounterfactualObservation,
) -> str:
    body = {
        "artifact_digest": observation.artifact_digest,
        "backend_evidence_digest": observation.backend_evidence_digest,
        "definition_digest": definition_digest(scenario.definition),
        "plan_digest": observation.plan_digest,
        "scenario_id": scenario.definition.scenario_id,
    }
    return "sha256:" + hashlib.sha256(
        b"doc2harness.extension-counterfactual-evaluation/1\0" + canonical_json(body)
    ).hexdigest()


__all__ = [
    "ExtensionApplicability",
    "ExtensionCounterfactualEvaluation",
    "ExtensionCounterfactualObservation",
    "ExtensionCounterfactualObserver",
    "ExtensionCounterfactualRecipe",
    "ExtensionCounterfactualScenario",
    "ExtensionCaptureStep",
    "ExtensionFace",
    "ExtensionFixtureStep",
    "ExtensionOutcome",
    "ExtensionRunObservation",
    "ExtensionScenarioStep",
    "ExtensionSetupStep",
    "ExtensionToolRoundStep",
    "capture_extension_face",
    "compile_extension_steps",
    "define_extension_scenario",
    "materialize_symlink_fixture",
    "setup_command",
    "tool_round",
    "evaluation_digest",
]
