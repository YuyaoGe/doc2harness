"""Typed recipes and raw observations for interactive TUI PTY scenarios."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from enum import Enum

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
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_HEX = re.compile(r"(?:[0-9a-f]{2})*\Z", re.ASCII)
_MAX_TEXT = 4_000_000
_MAX_ITEMS = 4096


def _text(value: object, name: str, *, empty: bool = True) -> None:
    if type(value) is not str or len(value) > _MAX_TEXT or (not empty and not value):
        raise ContractDocumentError(f"TUI PTY {name} is invalid")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ContractDocumentError(f"TUI PTY {name} is invalid")


def _hex(value: object, name: str) -> None:
    if type(value) is not str or len(value) > _MAX_TEXT * 2 or _HEX.fullmatch(value) is None:
        raise ContractDocumentError(f"TUI PTY {name} is invalid")


def _strings(value: object, name: str, *, unique: bool = False) -> None:
    if (
        type(value) is not tuple
        or len(value) > _MAX_ITEMS
        or any(type(item) is not str or len(item) > _MAX_TEXT for item in value)
        or (unique and len(set(value)) != len(value))
    ):
        raise ContractDocumentError(f"TUI PTY {name} is invalid")


def _relative(value: object, name: str) -> None:
    _text(value, name, empty=False)
    if value.startswith("/") or ".." in value.split("/"):
        raise ContractDocumentError(f"TUI PTY {name} must be relative")


class TuiProfile(str, Enum):
    CHAOS_SURVIVES = "chk_survives_chaos"
    FILE_WRITTEN = "chk_tui_file"
    WIRE_SURVIVES = "chk_tui_wire_survives"
    HISTORY_FRESH = "chk_history_fresh_control"
    HISTORY_REALPATH = "chk_history_realpath_key"


class TuiOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class TuiApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


class HistoryScenario(str, Enum):
    NONE = ""
    FRESH_A = "history-fresh-a"
    FRESH_B = "history-fresh-b"
    REALPATH_KEY = "history-realpath-key"


@dataclass(frozen=True, slots=True)
class TuiInput:
    data_hex: str

    def __post_init__(self) -> None:
        _hex(self.data_hex, "input bytes")
        if not self.data_hex:
            raise ContractDocumentError("TUI PTY input bytes must not be empty")


@dataclass(frozen=True, slots=True)
class SeedFile:
    path: str
    data_hex: str

    def __post_init__(self) -> None:
        _relative(self.path, "seed path")
        _hex(self.data_hex, "seed bytes")


@dataclass(frozen=True, slots=True)
class TuiOracleBinding:
    primary_token: str = ""
    secondary_token: str = ""
    foreign_token: str = ""
    line_token: str = ""
    path: str = ""
    specification: str = ""
    history_prefix: str = ""
    completion_token: str = "TUILINE_DONE_7755"
    crash_signatures: tuple[str, ...] = (
        "Traceback (most recent call last)",
        "UnhandledError",
        "panic:",
        "Unhandled exception",
        "RuntimeError:",
        "KeyError:",
        "AttributeError:",
        "ValueError:",
    )

    def __post_init__(self) -> None:
        for value in (
            self.primary_token,
            self.secondary_token,
            self.foreign_token,
            self.line_token,
            self.specification,
            self.history_prefix,
            self.completion_token,
        ):
            _text(value, "oracle text")
        if self.path:
            _relative(self.path, "oracle path")
        _strings(self.crash_signatures, "crash signatures", unique=True)


@dataclass(frozen=True, slots=True)
class TuiRecipe:
    profile: TuiProfile
    case_digest: str
    dependencies: tuple[tuple[str, DependencyKind], ...]
    timeout_seconds: int
    blocking: bool
    input_lines: tuple[TuiInput, ...] = ()
    seed_files: tuple[SeedFile, ...] = ()
    gateway_mode: str = "normal"
    reply_content: str = "ack"
    probe_tokens: tuple[str, ...] = ()
    cli_arguments: tuple[str, ...] = ()
    observed_files: tuple[str, ...] = ()
    history_scenario: HistoryScenario = HistoryScenario.NONE
    oracle: TuiOracleBinding = TuiOracleBinding()

    def __post_init__(self) -> None:
        if type(self.profile) is not TuiProfile:
            raise ContractDocumentError("TUI PTY profile is invalid")
        _digest(self.case_digest, "case digest")
        if type(self.dependencies) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _ID.fullmatch(item[0]) is None
            or type(item[1]) is not DependencyKind
            for item in self.dependencies
        ):
            raise ContractDocumentError("TUI PTY dependencies are invalid")
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 600
            or type(self.blocking) is not bool
        ):
            raise ContractDocumentError("TUI PTY execution policy is invalid")
        if type(self.input_lines) is not tuple or any(
            type(item) is not TuiInput for item in self.input_lines
        ):
            raise ContractDocumentError("TUI PTY inputs are invalid")
        if type(self.seed_files) is not tuple or any(
            type(item) is not SeedFile for item in self.seed_files
        ):
            raise ContractDocumentError("TUI PTY seed files are invalid")
        _text(self.gateway_mode, "gateway mode", empty=False)
        _text(self.reply_content, "reply content")
        _strings(self.probe_tokens, "probe tokens", unique=True)
        _strings(self.cli_arguments, "CLI arguments")
        _strings(self.observed_files, "observed files", unique=True)
        for path in self.observed_files:
            _relative(path, "observed path")
        if type(self.history_scenario) is not HistoryScenario:
            raise ContractDocumentError("TUI PTY history scenario is invalid")
        if type(self.oracle) is not TuiOracleBinding:
            raise ContractDocumentError("TUI PTY oracle binding is invalid")


@dataclass(frozen=True, slots=True)
class TuiRequest:
    body: str

    def __post_init__(self) -> None:
        _text(self.body, "request body")


@dataclass(frozen=True, slots=True)
class ObservedFile:
    path: str
    present: bool
    content: str = ""

    def __post_init__(self) -> None:
        _relative(self.path, "observed file path")
        if type(self.present) is not bool:
            raise ContractDocumentError("TUI PTY file presence is invalid")
        _text(self.content, "observed file content")
        if not self.present and self.content:
            raise ContractDocumentError("absent TUI PTY file cannot have content")


@dataclass(frozen=True, slots=True)
class PromptObservation:
    identity: str
    turn_id: str
    candidate_boundary: bool
    canary_nonce_absent_before: bool
    canary_nonce_absent_from_input: bool
    canary_observed: bool
    input_consumed: bool
    startup_failure_kind: str = ""
    startup_returncode: int | None = None
    startup_signal: int | None = None
    timeline: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.identity, "prompt identity", empty=False)
        _text(self.turn_id, "prompt turn", empty=False)
        _text(self.startup_failure_kind, "prompt startup failure")
        if any(
            type(value) is not bool
            for value in (
                self.candidate_boundary,
                self.canary_nonce_absent_before,
                self.canary_nonce_absent_from_input,
                self.canary_observed,
                self.input_consumed,
            )
        ):
            raise ContractDocumentError("TUI PTY prompt facts are invalid")
        for value in (self.startup_returncode, self.startup_signal):
            if value is not None and type(value) is not int:
                raise ContractDocumentError("TUI PTY prompt terminal fact is invalid")
        _strings(self.timeline, "prompt timeline")


@dataclass(frozen=True, slots=True)
class HistoryFile:
    path: str
    data_hex: str

    def __post_init__(self) -> None:
        _relative(self.path, "history path")
        _hex(self.data_hex, "history bytes")


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    complete: bool
    files: tuple[HistoryFile, ...]

    def __post_init__(self) -> None:
        if type(self.complete) is not bool or type(self.files) is not tuple or any(
            type(item) is not HistoryFile for item in self.files
        ):
            raise ContractDocumentError("TUI PTY history snapshot is invalid")
        paths = tuple(item.path for item in self.files)
        if len(paths) != len(set(paths)):
            raise ContractDocumentError("TUI PTY history paths must be unique")


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    name: str
    kind: str
    input_hex: str
    output_text: str
    output_sha256: str
    output_size: int
    requests_before: int
    requests_after: int
    returncode: int | None = None

    def __post_init__(self) -> None:
        _text(self.name, "history event name", empty=False)
        _text(self.kind, "history event kind", empty=False)
        _hex(self.input_hex, "history event input")
        _text(self.output_text, "history event output")
        if type(self.output_sha256) is not str or re.fullmatch(
            r"[0-9a-f]{64}", self.output_sha256
        ) is None:
            raise ContractDocumentError("TUI PTY history output digest is invalid")
        if any(
            type(value) is not int or value < 0
            for value in (self.output_size, self.requests_before, self.requests_after)
        ) or (self.returncode is not None and type(self.returncode) is not int):
            raise ContractDocumentError("TUI PTY history event counters are invalid")


@dataclass(frozen=True, slots=True)
class HistoryObservation:
    scenario: HistoryScenario
    workspace: str = ""
    workspace_a: str = ""
    workspace_b: str = ""
    writer: tuple[HistoryEvent, ...] = ()
    reader: tuple[HistoryEvent, ...] = ()
    writer_a: tuple[HistoryEvent, ...] = ()
    writer_b: tuple[HistoryEvent, ...] = ()
    reader_a: tuple[HistoryEvent, ...] = ()
    reader_b: tuple[HistoryEvent, ...] = ()
    snapshot: HistorySnapshot | None = None
    snapshot_a: HistorySnapshot | None = None
    snapshot_ab: HistorySnapshot | None = None

    def __post_init__(self) -> None:
        if type(self.scenario) is not HistoryScenario:
            raise ContractDocumentError("TUI PTY history identity is invalid")
        for value in (self.workspace, self.workspace_a, self.workspace_b):
            _text(value, "history workspace")
        for values in (
            self.writer,
            self.reader,
            self.writer_a,
            self.writer_b,
            self.reader_a,
            self.reader_b,
        ):
            if type(values) is not tuple or any(
                type(item) is not HistoryEvent for item in values
            ):
                raise ContractDocumentError("TUI PTY history events are invalid")
            names = tuple(item.name for item in values)
            if len(names) != len(set(names)):
                raise ContractDocumentError("TUI PTY history event names must be unique")
        for value in (self.snapshot, self.snapshot_a, self.snapshot_ab):
            if value is not None and type(value) is not HistorySnapshot:
                raise ContractDocumentError("TUI PTY history snapshot type is invalid")


@dataclass(frozen=True, slots=True)
class TuiScenario:
    definition: ScenarioDefinition
    recipe: TuiRecipe

    def __post_init__(self) -> None:
        if type(self.definition) is not ScenarioDefinition or type(self.recipe) is not TuiRecipe:
            raise ContractDocumentError("TUI PTY scenario requires exact typed values")
        if self.definition.classification.primary != "tui":
            raise ContractDocumentError("TUI PTY scenario owner differs from its Harness")


@dataclass(frozen=True, slots=True)
class TuiObservation:
    scenario_id: str
    profile: TuiProfile
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    backend_evidence_digest: str
    cli_available: bool
    prompt: PromptObservation
    screen_text: str
    screen_sha256: str
    screen_size: int
    returncode: int | None
    signal: int | None
    requests: tuple[TuiRequest, ...]
    files: tuple[ObservedFile, ...]
    history: HistoryObservation | None = None
    elapsed_ns: int = 0
    deadline_reached: bool = False
    driver_error: str = ""
    process_tree_clean: bool = True
    workspace_clean: bool = True
    trajectory_text: str = ""

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("TUI PTY observation scenario is invalid")
        if type(self.profile) is not TuiProfile:
            raise ContractDocumentError("TUI PTY observation profile is invalid")
        for value in (
            self.plan_digest,
            self.artifact_digest,
            self.backend_evidence_digest,
        ):
            _digest(value, "evidence digest")
        _text(self.artifact_id, "artifact id", empty=False)
        if type(self.cli_available) is not bool or type(self.prompt) is not PromptObservation:
            raise ContractDocumentError("TUI PTY observation availability is invalid")
        _text(self.screen_text, "screen text")
        if type(self.screen_sha256) is not str or re.fullmatch(
            r"[0-9a-f]{64}", self.screen_sha256
        ) is None:
            raise ContractDocumentError("TUI PTY screen digest is invalid")
        if type(self.screen_size) is not int or self.screen_size < 0:
            raise ContractDocumentError("TUI PTY screen size is invalid")
        for value in (self.returncode, self.signal):
            if value is not None and type(value) is not int:
                raise ContractDocumentError("TUI PTY terminal status is invalid")
        if type(self.requests) is not tuple or any(
            type(item) is not TuiRequest for item in self.requests
        ):
            raise ContractDocumentError("TUI PTY requests are invalid")
        if type(self.files) is not tuple or any(
            type(item) is not ObservedFile for item in self.files
        ):
            raise ContractDocumentError("TUI PTY files are invalid")
        if self.history is not None and type(self.history) is not HistoryObservation:
            raise ContractDocumentError("TUI PTY history is invalid")
        if type(self.elapsed_ns) is not int or self.elapsed_ns < 0:
            raise ContractDocumentError("TUI PTY elapsed time is invalid")
        if any(
            type(value) is not bool
            for value in (
                self.deadline_reached,
                self.process_tree_clean,
                self.workspace_clean,
            )
        ):
            raise ContractDocumentError("TUI PTY cleanup facts are invalid")
        _text(self.driver_error, "driver error")
        _text(self.trajectory_text, "trajectory text")
        paths = tuple(item.path for item in self.files)
        if len(paths) != len(set(paths)):
            raise ContractDocumentError("TUI PTY observed files must be unique")


@dataclass(frozen=True, slots=True)
class TuiEvaluation:
    scenario_id: str
    outcome: TuiOutcome
    applicability: TuiApplicability
    reason_code: str
    evidence_digest: str
    contribution_microunits: int


def define_tui_scenario(
    scenario_id: str, recipe: TuiRecipe, role: ScenarioRole, /
) -> TuiScenario:
    dependencies = tuple(
        sorted(
            (DependencyRef(test_id, kind) for test_id, kind in recipe.dependencies),
            key=lambda item: (item.scenario_id, item.kind.value),
        )
    )
    definition = ScenarioDefinition(
        scenario_id,
        "1",
        Classification("tui"),
        role,
        1_000_000 if role is ScenarioRole.TARGET else 0,
        ScenarioDifficulty.HARD,
        dependencies,
        ExperimentBinding(
            ComponentRef("tui-pty-fixture", "1", ComponentKind.FIXTURE),
            ComponentRef("tui-pty-adapter", "1", ComponentKind.STIMULUS),
            ComponentRef("tui-pty-observer", "1", ComponentKind.OBSERVER),
            ComponentRef("tui-pty-oracle", "1", ComponentKind.ORACLE),
            ComponentRef("tui-pty-cleanup", "1", ComponentKind.CLEANUP),
        ),
        UnsupportedPolicy.FAIL,
    )
    return TuiScenario(definition, recipe)


def recipe_digest(recipe: TuiRecipe, /) -> str:
    if type(recipe) is not TuiRecipe:
        raise ContractDocumentError("TUI PTY recipe digest requires an exact recipe")
    return "sha256:" + hashlib.sha256(
        b"doc2harness.tui-pty-recipe/1\0" + canonical_json(asdict(recipe))
    ).hexdigest()


def evaluation_digest(scenario: TuiScenario, observation: TuiObservation) -> str:
    body = {
        "artifact_digest": observation.artifact_digest,
        "backend_evidence_digest": observation.backend_evidence_digest,
        "definition_digest": definition_digest(scenario.definition),
        "plan_digest": observation.plan_digest,
        "scenario_id": scenario.definition.scenario_id,
    }
    return "sha256:" + hashlib.sha256(
        b"doc2harness.tui-pty-evaluation/1\0" + canonical_json(body)
    ).hexdigest()
