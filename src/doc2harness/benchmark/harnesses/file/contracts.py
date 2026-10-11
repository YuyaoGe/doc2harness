"""Closed declarations and observations for child-process file-tool scenarios."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

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
JsonAtom: TypeAlias = str | int | bool | None
FrozenValue: TypeAlias = (
    JsonAtom | tuple["FrozenValue", ...] | tuple[tuple[str, "FrozenValue"], ...]
)


class FileToolOracleProfile(str, Enum):
    TEXT_RULES = "TEXT_RULES"
    TRANSITION_EXACT = "TRANSITION_EXACT"
    REWIND_RESTORES = "REWIND_RESTORES"
    NOTEBOOK_ISOLATED = "NOTEBOOK_ISOLATED"
    MEMORY_EVENT = "MEMORY_EVENT"


class FileToolOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class FileToolApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class FileToolJudgement:
    """The small, typed result returned by a scenario-local judge."""

    outcome: FileToolOutcome
    applicability: FileToolApplicability
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not FileToolOutcome:
            raise ContractDocumentError("file-tool judgement outcome is invalid")
        if type(self.applicability) is not FileToolApplicability:
            raise ContractDocumentError("file-tool judgement applicability is invalid")
        if type(self.reason_code) is not str or not self.reason_code:
            raise ContractDocumentError("file-tool judgement reason is invalid")


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or any(
        type(item) is not str or not item for item in value
    ):
        raise ContractDocumentError(f"file-tool {label} is invalid")
    return value


def _validate_frozen(value: FrozenValue, depth: int = 0) -> None:
    if depth > 16:
        raise ContractDocumentError("file-tool frozen value nesting is excessive")
    if value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > 8 * 1024 * 1024:
            raise ContractDocumentError("file-tool frozen text is excessive")
        return
    if type(value) is not tuple or len(value) > 4096:
        raise ContractDocumentError("file-tool frozen value is invalid")
    for item in value:
        if type(item) is tuple and len(item) == 2 and type(item[0]) is str:
            _validate_frozen(item[1], depth + 1)
        else:
            _validate_frozen(item, depth + 1)


@dataclass(frozen=True, slots=True)
class FileToolCall:
    tool_names: tuple[str, ...]
    arguments: tuple[tuple[str, FrozenValue], ...]
    aliases_explicit: bool
    arguments_nested: bool

    def __post_init__(self) -> None:
        _text_tuple(self.tool_names, "tool names")
        if len(set(self.tool_names)) != len(self.tool_names):
            raise ContractDocumentError("file-tool aliases must be unique")
        if type(self.arguments) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
            for item in self.arguments
        ):
            raise ContractDocumentError("file-tool arguments are invalid")
        if len({item[0] for item in self.arguments}) != len(self.arguments):
            raise ContractDocumentError("file-tool argument names must be unique")
        for _, value in self.arguments:
            _validate_frozen(value)
        if (
            type(self.aliases_explicit) is not bool
            or type(self.arguments_nested) is not bool
        ):
            raise ContractDocumentError("file-tool call encoding policy is invalid")


@dataclass(frozen=True, slots=True)
class FileToolFixtureStep:
    """Declare one path-independent file fixture for a scenario."""

    path: str
    content: str

    def __post_init__(self) -> None:
        if (
            type(self.path) is not str
            or not self.path
            or self.path.startswith("/")
            or ".." in self.path.split("/")
            or type(self.content) is not str
        ):
            raise ContractDocumentError("file-tool fixture step is invalid")


@dataclass(frozen=True, slots=True)
class FileToolSetupStep:
    """Declare one setup command sent before the tool rounds."""

    command: str

    def __post_init__(self) -> None:
        if type(self.command) is not str or not self.command:
            raise ContractDocumentError("file-tool setup step is invalid")


@dataclass(frozen=True, slots=True)
class FileToolRoundStep:
    """Declare one tool call and its one-based gateway turn."""

    turn: int
    call: FileToolCall

    def __post_init__(self) -> None:
        if type(self.turn) is not int or self.turn < 1 or type(self.call) is not FileToolCall:
            raise ContractDocumentError("file-tool round step is invalid")


@dataclass(frozen=True, slots=True)
class FileToolCaptureStep:
    """Declare the model-text rules used by a scenario-local judge."""

    source: str
    required: tuple[str, ...]
    forbidden: tuple[str, ...] = ()
    min_requests: int = 1
    required_regex: str | None = None
    forbidden_regex: str | None = None
    stderr_forbidden: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.source) is not str or not self.source:
            raise ContractDocumentError("file-tool capture source is invalid")
        _text_tuple(self.required, "capture required rules")
        _text_tuple(self.forbidden, "capture forbidden rules")
        _text_tuple(self.stderr_forbidden, "capture stderr rules")
        if type(self.min_requests) is not int or self.min_requests < 0:
            raise ContractDocumentError("file-tool capture request bound is invalid")
        for name, value in (
            ("required regex", self.required_regex),
            ("forbidden regex", self.forbidden_regex),
        ):
            if value is not None and (type(value) is not str or not value):
                raise ContractDocumentError(f"file-tool {name} is invalid")


FileToolScenarioStep = (
    FileToolFixtureStep
    | FileToolSetupStep
    | FileToolRoundStep
    | FileToolCaptureStep
)


def write_fixture(path: str, content: str) -> FileToolFixtureStep:
    """Declare one seeded workspace file."""

    return FileToolFixtureStep(path, content)


def setup_command(command: str) -> FileToolSetupStep:
    """Declare one setup command."""

    return FileToolSetupStep(command)


def tool_round(
    tool: str | tuple[str, ...],
    *,
    turn: int,
    aliases_explicit: bool = False,
    arguments_nested: bool = False,
    **arguments: FrozenValue,
) -> FileToolRoundStep:
    """Declare a tool call without exposing the internal tuple encoding."""

    names = (tool,) if type(tool) is str else tool
    return FileToolRoundStep(
        turn,
        FileToolCall(
            names,
            tuple(arguments.items()),
            aliases_explicit,
            arguments_nested,
        ),
    )


def capture_model_text(
    *,
    required: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
    min_requests: int = 1,
    required_regex: str | None = None,
    forbidden_regex: str | None = None,
    stderr_forbidden: tuple[str, ...] = (),
) -> FileToolCaptureStep:
    """Declare bounded text facts consumed by a local judge."""

    return capture_fact(
        "model_text",
        required=required,
        forbidden=forbidden,
        min_requests=min_requests,
        required_regex=required_regex,
        forbidden_regex=forbidden_regex,
        stderr_forbidden=stderr_forbidden,
    )


def capture_fact(
    source: str,
    *,
    required: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
    min_requests: int = 1,
    required_regex: str | None = None,
    forbidden_regex: str | None = None,
    stderr_forbidden: tuple[str, ...] = (),
) -> FileToolCaptureStep:
    """Declare bounded rules against one named observation fact."""

    return FileToolCaptureStep(
        source,
        required,
        forbidden,
        min_requests,
        required_regex,
        forbidden_regex,
        stderr_forbidden,
    )


@dataclass(frozen=True, slots=True)
class FileToolRecipe:
    case_kind: str
    declared_tools: tuple[str, ...]
    setup_commands: tuple[str, ...]
    seed_files: tuple[tuple[str, str], ...]
    rounds: tuple[tuple[FileToolCall, ...], ...]
    oracle_profile: FileToolOracleProfile
    oracle_parameters: tuple[tuple[str, FrozenValue], ...]
    dependencies: tuple[tuple[str, DependencyKind], ...] = ()
    blocking: bool = False
    timeout_seconds: int = 90
    execution_parameters: tuple[tuple[str, FrozenValue], ...] = ()
    bind_oracle_parameters: bool = False

    def __post_init__(self) -> None:
        if type(self.case_kind) is not str or not self.case_kind:
            raise ContractDocumentError("file-tool case kind is invalid")
        _text_tuple(self.declared_tools, "declared tools")
        _text_tuple(self.setup_commands, "setup commands")
        if type(self.seed_files) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or not item[0]
            or item[0].startswith("/")
            or ".." in item[0].split("/")
            or type(item[1]) is not str
            for item in self.seed_files
        ):
            raise ContractDocumentError("file-tool seeds are invalid")
        if type(self.rounds) is not tuple or any(
            type(turn) is not tuple
            or not turn
            or any(type(call) is not FileToolCall for call in turn)
            for turn in self.rounds
        ):
            raise ContractDocumentError("file-tool rounds are invalid")
        if type(self.execution_parameters) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
            for item in self.execution_parameters
        ):
            raise ContractDocumentError("file-tool execution parameters are invalid")
        if len({item[0] for item in self.execution_parameters}) != len(
            self.execution_parameters
        ):
            raise ContractDocumentError(
                "file-tool execution parameter names must be unique"
            )
        for _, value in self.execution_parameters:
            _validate_frozen(value)
        if not self.rounds and not dict(self.execution_parameters).get("phase1_rounds"):
            raise ContractDocumentError("file-tool rounds are invalid")
        if type(self.oracle_profile) is not FileToolOracleProfile:
            raise ContractDocumentError("file-tool oracle profile is invalid")
        if type(self.oracle_parameters) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
            for item in self.oracle_parameters
        ):
            raise ContractDocumentError("file-tool oracle parameters are invalid")
        for _, value in self.oracle_parameters:
            _validate_frozen(value)
        if type(self.dependencies) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _ID.fullmatch(item[0]) is None
            or type(item[1]) is not DependencyKind
            for item in self.dependencies
        ):
            raise ContractDocumentError("file-tool dependency id is invalid")
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 900
        ):
            raise ContractDocumentError("file-tool timeout is invalid")
        if type(self.blocking) is not bool:
            raise ContractDocumentError("file-tool blocking policy is invalid")
        if type(self.bind_oracle_parameters) is not bool:
            raise ContractDocumentError("file-tool oracle binding policy is invalid")


def compile_file_tool_steps(
    steps: tuple[FileToolScenarioStep, ...],
    /,
    *,
    case_kind: str,
    declared_tools: tuple[str, ...],
    oracle_profile: FileToolOracleProfile,
    dependencies: tuple[tuple[str, DependencyKind], ...] = (),
    blocking: bool = False,
    timeout_seconds: int = 90,
    execution_parameters: tuple[tuple[str, FrozenValue], ...] = (),
    bind_oracle_parameters: bool = False,
) -> FileToolRecipe:
    """Compile readable fixture/setup/round/capture steps into a recipe."""

    if type(steps) is not tuple or not steps:
        raise ContractDocumentError("file-tool steps must be a non-empty tuple")
    cursor = 0
    seed_files: list[tuple[str, str]] = []
    while cursor < len(steps) and type(steps[cursor]) is FileToolFixtureStep:
        fixture = steps[cursor]
        seed_files.append((fixture.path, fixture.content))
        cursor += 1
    if len({path for path, _ in seed_files}) != len(seed_files):
        raise ContractDocumentError("file-tool fixture paths must be unique")

    setup_commands: list[str] = []
    while cursor < len(steps) and type(steps[cursor]) is FileToolSetupStep:
        setup_commands.append(steps[cursor].command)
        cursor += 1

    round_steps: list[FileToolRoundStep] = []
    while cursor < len(steps) and type(steps[cursor]) is FileToolRoundStep:
        round_steps.append(steps[cursor])
        cursor += 1
    if not round_steps:
        raise ContractDocumentError("file-tool steps require at least one round")
    rounds: list[list[FileToolCall]] = []
    expected_turn = 1
    for step in round_steps:
        if step.turn < expected_turn or step.turn > expected_turn + 1:
            raise ContractDocumentError("file-tool round turns are not contiguous")
        if step.turn == expected_turn + 1:
            rounds.append([])
            expected_turn += 1
        if not rounds:
            rounds.append([])
        rounds[-1].append(step.call)

    captures = steps[cursor:]
    if len(captures) != 1 or type(captures[0]) is not FileToolCaptureStep:
        raise ContractDocumentError("file-tool steps require one final capture")
    capture = captures[0]
    oracle_parameters: list[tuple[str, FrozenValue]] = [
        ("source", capture.source),
        ("required", capture.required),
        ("forbidden", capture.forbidden),
        ("min_requests", capture.min_requests),
    ]
    optional_parameters = (
        ("required_regex", capture.required_regex),
        ("forbidden_regex", capture.forbidden_regex),
        ("stderr_forbidden", capture.stderr_forbidden),
    )
    oracle_parameters.extend(
        (name, value)
        for name, value in optional_parameters
        if value is not None and (name != "stderr_forbidden" or value)
    )
    return FileToolRecipe(
        case_kind,
        declared_tools,
        tuple(setup_commands),
        tuple(seed_files),
        tuple(tuple(turn) for turn in rounds),
        oracle_profile,
        tuple(oracle_parameters),
        dependencies,
        blocking,
        timeout_seconds,
        execution_parameters,
        bind_oracle_parameters,
    )


@dataclass(frozen=True, slots=True)
class FileToolScenario:
    definition: ScenarioDefinition
    recipe: FileToolRecipe

    def __post_init__(self) -> None:
        if (
            type(self.definition) is not ScenarioDefinition
            or type(self.recipe) is not FileToolRecipe
        ):
            raise ContractDocumentError(
                "file-tool scenario requires exact typed values"
            )
        if self.definition.classification.primary != "file":
            raise ContractDocumentError(
                "file-tool scenario owner differs from its Harness layer"
            )


@dataclass(frozen=True, slots=True)
class FileToolRawObservation:
    scenario_id: str
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    request_count: int
    advertised_tools: tuple[str, ...]
    live: bool
    target_content: str | None
    secondary_target_content: str | None
    model_text: str
    stderr_text: str
    empty_tool_result: bool | None
    process_tree_clean: bool
    gateway_clean: bool
    workspace_clean: bool
    timed_out: bool
    backend_evidence_digest: str
    target_tool_result: str = ""
    round_target_tool_results: tuple[str, ...] = ()
    facts: tuple[tuple[str, FrozenValue], ...] = ()

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("file-tool observation id is invalid")
        for digest in (
            self.plan_digest,
            self.artifact_digest,
            self.backend_evidence_digest,
        ):
            if (
                type(digest) is not str
                or not digest.startswith("sha256:")
                or len(digest) != 71
            ):
                raise ContractDocumentError("file-tool observation digest is invalid")
        if type(self.artifact_id) is not str or _ID.fullmatch(self.artifact_id) is None:
            raise ContractDocumentError("file-tool artifact id is invalid")
        if type(self.request_count) is not int or not 0 <= self.request_count <= 4096:
            raise ContractDocumentError("file-tool request count is invalid")
        _text_tuple(self.advertised_tools, "advertised tools")
        for value in (
            self.live,
            self.process_tree_clean,
            self.gateway_clean,
            self.workspace_clean,
            self.timed_out,
        ):
            if type(value) is not bool:
                raise ContractDocumentError("file-tool boolean fact is invalid")
        for value in (self.target_content, self.secondary_target_content):
            if value is not None and type(value) is not str:
                raise ContractDocumentError("file-tool target content is invalid")
        if type(self.model_text) is not str or type(self.stderr_text) is not str:
            raise ContractDocumentError("file-tool text fact is invalid")
        if type(self.target_tool_result) is not str:
            raise ContractDocumentError("file-tool target-result fact is invalid")
        _text_tuple(self.round_target_tool_results, "round target results")
        if type(self.facts) is not tuple or any(
            type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
            for item in self.facts
        ):
            raise ContractDocumentError("file-tool evidence facts are invalid")
        if len({item[0] for item in self.facts}) != len(self.facts):
            raise ContractDocumentError("file-tool evidence fact names must be unique")
        for _, value in self.facts:
            _validate_frozen(value)
        if (
            self.empty_tool_result is not None
            and type(self.empty_tool_result) is not bool
        ):
            raise ContractDocumentError("file-tool empty-result fact is invalid")


@dataclass(frozen=True, slots=True)
class FileToolEvaluation:
    scenario_id: str
    outcome: FileToolOutcome
    applicability: FileToolApplicability
    reason_code: str
    evidence_digest: str
    contribution_microunits: int

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("file-tool evaluation id is invalid")
        if (
            type(self.outcome) is not FileToolOutcome
            or type(self.applicability) is not FileToolApplicability
        ):
            raise ContractDocumentError("file-tool evaluation closed value is invalid")
        if (
            type(self.reason_code) is not str
            or not self.reason_code
            or len(self.reason_code) > 128
        ):
            raise ContractDocumentError("file-tool evaluation reason is invalid")
        if (
            type(self.evidence_digest) is not str
            or not self.evidence_digest.startswith("sha256:")
            or len(self.evidence_digest) != 71
        ):
            raise ContractDocumentError("file-tool evaluation digest is invalid")
        if self.contribution_microunits not in {0, 1_000_000}:
            raise ContractDocumentError("file-tool evaluation contribution is invalid")


def define_file_tool_scenario(
    scenario_id: str, recipe: FileToolRecipe, role: ScenarioRole, /
) -> FileToolScenario:
    if type(scenario_id) is not str or _ID.fullmatch(scenario_id) is None:
        raise ContractDocumentError("file-tool scenario id is invalid")
    dependencies = tuple(
        sorted(
            (DependencyRef(item, kind) for item, kind in recipe.dependencies),
            key=lambda item: (item.scenario_id, item.kind.value),
        )
    )
    oracle_component_id = "file-tool-child-oracle"
    if recipe.bind_oracle_parameters:
        oracle_binding = hashlib.sha256(
            b"doc2harness.file-tool-child-oracle-binding/1\0"
            + canonical_json(
                {
                    "parameters": list(recipe.oracle_parameters),
                    "profile": recipe.oracle_profile.value,
                }
            )
        ).hexdigest()
        oracle_component_id += "-" + oracle_binding
    definition = ScenarioDefinition(
        scenario_id,
        "1",
        Classification("file"),
        role,
        1_000_000 if role is ScenarioRole.TARGET else 0,
        ScenarioDifficulty.HARD,
        dependencies,
        ExperimentBinding(
            ComponentRef("file-tool-child-fixture", "1", ComponentKind.FIXTURE),
            ComponentRef("file-tool-child-adapter", "1", ComponentKind.STIMULUS),
            ComponentRef("file-tool-child-observer", "1", ComponentKind.OBSERVER),
            ComponentRef(oracle_component_id, "1", ComponentKind.ORACLE),
            ComponentRef("file-tool-child-cleanup", "1", ComponentKind.CLEANUP),
        ),
        UnsupportedPolicy.FAIL,
    )
    return FileToolScenario(definition, recipe)


def evaluation_digest(
    scenario: FileToolScenario, observation: FileToolRawObservation
) -> str:
    body = {
        "artifact_digest": observation.artifact_digest,
        "backend_evidence_digest": observation.backend_evidence_digest,
        "definition_digest": definition_digest(scenario.definition),
        "plan_digest": observation.plan_digest,
        "scenario_id": scenario.definition.scenario_id,
    }
    return (
        "sha256:"
        + hashlib.sha256(
            b"doc2harness.file-tool-child-evaluation/1\0" + canonical_json(body)
        ).hexdigest()
    )


def validate_file_tool_observation(
    scenario: FileToolScenario, observation: FileToolRawObservation, /
) -> None:
    """Validate the typed boundary before a scenario-local judge runs."""

    if type(scenario) is not FileToolScenario:
        raise ContractDocumentError("file-tool judge requires an exact scenario")
    if type(observation) is not FileToolRawObservation:
        raise ContractDocumentError("file-tool judge requires typed observation")
    if observation.scenario_id != scenario.definition.scenario_id:
        raise ContractDocumentError("file-tool observation belongs to another scenario")


def build_file_tool_evaluation(
    scenario: FileToolScenario,
    observation: FileToolRawObservation,
    judgement: FileToolJudgement,
    /,
) -> FileToolEvaluation:
    """Build the evaluation from a local typed judgement."""

    validate_file_tool_observation(scenario, observation)
    if type(judgement) is not FileToolJudgement:
        raise ContractDocumentError("file-tool judge returned an invalid judgement")
    contribution = 1_000_000 if (
        scenario.definition.role is ScenarioRole.TARGET
        and judgement.applicability is FileToolApplicability.APPLICABLE
        and judgement.outcome is FileToolOutcome.PASS
    ) else 0
    return FileToolEvaluation(
        scenario.definition.scenario_id,
        judgement.outcome,
        judgement.applicability,
        judgement.reason_code,
        evaluation_digest(scenario, observation),
        contribution,
    )


__all__ = [
    name
    for name in globals()
    if name.startswith("FileTool")
    or name
    in {
        "build_file_tool_evaluation",
        "capture_fact",
        "capture_model_text",
        "compile_file_tool_steps",
        "define_file_tool_scenario",
        "evaluation_digest",
        "python_ast_digest",
        "setup_command",
        "tool_round",
        "validate_file_tool_observation",
        "write_fixture",
    }
]
