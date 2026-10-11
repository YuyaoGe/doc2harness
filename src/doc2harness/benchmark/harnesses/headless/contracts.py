"""Typed execution recipes and raw observations for the headless CLI Harness."""

from __future__ import annotations

import hashlib
import json
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
_MAX_TEXT = 4_000_000
_MAX_ITEMS = 4096


def _text(value: object, name: str, *, allow_empty: bool = True) -> None:
    if (
        type(value) is not str
        or len(value) > _MAX_TEXT
        or (not allow_empty and not value)
    ):
        raise ContractDocumentError(f"headless {name} is invalid")


def _strings(value: object, name: str, *, unique: bool = False) -> None:
    if (
        type(value) is not tuple
        or len(value) > _MAX_ITEMS
        or any(type(v) is not str or len(v) > _MAX_TEXT for v in value)
    ):
        raise ContractDocumentError(f"headless {name} is invalid")
    if unique and len(set(value)) != len(value):
        raise ContractDocumentError(f"headless {name} must be unique")


def _relative_path(value: object, name: str, *, allow_empty: bool = True) -> None:
    _text(value, name, allow_empty=allow_empty)
    if value and (value.startswith("/") or ".." in value.split("/")):
        raise ContractDocumentError(f"headless {name} must be relative")


class HeadlessProfile(str, Enum):
    JSON_PURE = "chk_json_pure"
    JSON_ERROR_PURE = "chk_json_error_pure"
    PROBE_PRESENT = "chk_probe_present"
    PROBE_ABSENT = "chk_probe_absent"
    BUDGET_TYPE_DOMAIN_CONTROL = "chk_budget_type_domain_control"
    BUDGET_TYPE_DOMAIN = "chk_budget_type_domain"
    RUNTIME_ERROR_STDOUT_CONTROL = "chk_runtime_error_stdout_control"
    RUNTIME_ERROR_STDOUT = "chk_runtime_error_stdout"
    EXEC_BACKGROUND = "chk_exec_background"
    SESSION_FLAG_CONFLICT = "chk_session_flag_conflict"


class HeadlessExecutionMode(str, Enum):
    STANDARD = "standard"
    FORMAL_RUNTIME = "formal-runtime"
    FORMAL_CONTRACT = "formal-contract"


class HeadlessOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class HeadlessApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class HeadlessJudgement:
    """Typed result returned by one scenario-local, pure headless judge."""

    outcome: HeadlessOutcome
    applicability: HeadlessApplicability
    reason_code: str

    def __post_init__(self) -> None:
        if type(self.outcome) is not HeadlessOutcome:
            raise ContractDocumentError("headless judgement outcome is invalid")
        if type(self.applicability) is not HeadlessApplicability:
            raise ContractDocumentError("headless judgement applicability is invalid")
        _text(self.reason_code, "judgement reason", allow_empty=False)


@dataclass(frozen=True, slots=True)
class ArgumentVariant:
    label: str
    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label:
            raise ContractDocumentError("headless variant label is invalid")
        if type(self.arguments) is not tuple or any(
            type(v) is not str for v in self.arguments
        ):
            raise ContractDocumentError("headless variant arguments are invalid")


@dataclass(frozen=True, slots=True)
class SeedFile:
    path: str
    content: str

    def __post_init__(self) -> None:
        if (
            type(self.path) is not str
            or not self.path
            or self.path.startswith("/")
            or ".." in self.path.split("/")
        ):
            raise ContractDocumentError("headless seed path is invalid")
        if type(self.content) is not str:
            raise ContractDocumentError("headless seed content is invalid")


@dataclass(frozen=True, slots=True)
class ToolCall:
    names: tuple[str, ...]
    arguments_json: str = "{}"
    command: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.names) is not tuple
            or not self.names
            or any(type(v) is not str or not v for v in self.names)
        ):
            raise ContractDocumentError("headless tool names are invalid")
        if type(self.arguments_json) is not str or type(self.command) is not str:
            raise ContractDocumentError("headless tool payload is invalid")


@dataclass(frozen=True, slots=True)
class ToolRound:
    calls: tuple[ToolCall, ...]

    def __post_init__(self) -> None:
        if (
            type(self.calls) is not tuple
            or not self.calls
            or len(self.calls) > _MAX_ITEMS
            or any(type(v) is not ToolCall for v in self.calls)
        ):
            raise ContractDocumentError("headless tool round is invalid")


@dataclass(frozen=True, slots=True)
class SchemaProbe:
    label: str
    path: str
    key: str
    sentinel: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.label, "schema label"),
            (self.key, "schema key"),
            (self.sentinel, "schema sentinel"),
        ):
            _text(value, name, allow_empty=False)
        _relative_path(self.path, "schema path", allow_empty=False)


@dataclass(frozen=True, slots=True)
class JsonOverride:
    """One typed JSON field override, including deliberately invalid domains."""

    field: str
    canonical_value: str

    def __post_init__(self) -> None:
        _text(self.field, "JSON override field", allow_empty=False)
        _text(self.canonical_value, "JSON override value", allow_empty=False)
        try:
            value = json.loads(self.canonical_value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContractDocumentError("headless JSON override is invalid") from error
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if rendered != self.canonical_value:
            raise ContractDocumentError("headless JSON override is not canonical")


@dataclass(frozen=True, slots=True)
class FormalProbe:
    label: str
    budget: tuple[JsonOverride, ...] = ()
    path_role: str = ""
    task_overrides: tuple[JsonOverride, ...] = ()
    raw_contract: str = ""
    gateway_mode: str = ""
    environment: tuple[tuple[str, str], ...] = ()
    argv_overrides: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _text(self.label, "formal probe label", allow_empty=False)
        for value in (self.path_role, self.raw_contract, self.gateway_mode):
            _text(value, "formal probe text")
        if (
            type(self.budget) is not tuple
            or type(self.task_overrides) is not tuple
            or any(
                type(v) is not JsonOverride
                for v in (*self.budget, *self.task_overrides)
            )
        ):
            raise ContractDocumentError("headless formal JSON overrides are invalid")
        for values, name in (
            (self.environment, "formal environment"),
            (self.argv_overrides, "formal argv overrides"),
        ):
            if type(values) is not tuple or any(
                type(v) is not tuple
                or len(v) != 2
                or any(type(item) is not str for item in v)
                for v in values
            ):
                raise ContractDocumentError(f"headless {name} is invalid")
        if len({v.field for v in self.budget}) != len(self.budget) or len(
            {v.field for v in self.task_overrides}
        ) != len(self.task_overrides):
            raise ContractDocumentError("headless formal override keys must be unique")


@dataclass(frozen=True, slots=True)
class ListingProbe:
    label: str
    count: int
    payload_bytes: int

    def __post_init__(self) -> None:
        _text(self.label, "listing probe label", allow_empty=False)
        if (
            type(self.count) is not int
            or self.count <= 0
            or type(self.payload_bytes) is not int
            or self.payload_bytes <= 0
        ):
            raise ContractDocumentError("headless listing probe is invalid")


@dataclass(frozen=True, slots=True)
class CliFileProbe:
    label: str
    kind: str
    file_hex: str

    def __post_init__(self) -> None:
        _text(self.label, "CLI file probe label", allow_empty=False)
        _text(self.kind, "CLI file probe kind", allow_empty=False)
        if type(self.file_hex) is not str or len(self.file_hex) % 2:
            raise ContractDocumentError("headless CLI file bytes are invalid")
        try:
            bytes.fromhex(self.file_hex)
        except ValueError as error:
            raise ContractDocumentError("headless CLI file bytes are invalid") from error


@dataclass(frozen=True, slots=True)
class HeadlessOracleBinding:
    """Frozen checker closure values; these are oracle inputs, never verdicts."""

    sentinel: str = ""
    specification: str = ""
    baseline_label: str = ""
    subject_label: str = ""
    third_label: str = ""
    expected_tokens: tuple[str, ...] = ()
    expected_tools: tuple[str, ...] = ()
    expected_mark: str = ""
    expected_boolean: bool | None = None
    feature_name: str = ""
    step_cap: int = 0
    expected_relative_path: str = ""
    resume_subdirectory: str = ""
    schemas: tuple[SchemaProbe, ...] = ()

    def __post_init__(self) -> None:
        for value in (
            self.sentinel,
            self.specification,
            self.baseline_label,
            self.subject_label,
            self.third_label,
            self.expected_mark,
            self.feature_name,
            self.expected_relative_path,
            self.resume_subdirectory,
        ):
            _text(value, "oracle binding text")
        _strings(self.expected_tokens, "oracle expected tokens", unique=True)
        _strings(self.expected_tools, "oracle expected tools", unique=True)
        if (
            self.expected_boolean is not None
            and type(self.expected_boolean) is not bool
        ):
            raise ContractDocumentError("headless oracle expected boolean is invalid")
        if type(self.step_cap) is not int or self.step_cap < 0:
            raise ContractDocumentError("headless oracle step cap is invalid")
        if (
            type(self.schemas) is not tuple
            or len(self.schemas) > _MAX_ITEMS
            or any(type(v) is not SchemaProbe for v in self.schemas)
        ):
            raise ContractDocumentError("headless oracle schemas are invalid")
        if len({v.label for v in self.schemas}) != len(self.schemas):
            raise ContractDocumentError("headless oracle schema labels must be unique")


@dataclass(frozen=True, slots=True)
class HeadlessRecipe:
    """Complete typed stimulus; ``case_digest`` is audit-only."""

    profile: HeadlessProfile
    case_digest: str
    dependencies: tuple[tuple[str, DependencyKind], ...] = ()
    task: str = "Say the magic word and stop."
    output_format: str = "text"
    blocking: bool = True
    reply_content: str = "DONE_SENTINEL"
    gateway_mode: str = "fixed_reply"
    append_system_prompt: str = ""
    system_prompt: str = ""
    probe_token: str = ""
    probe_tokens: tuple[str, ...] = ()
    cli_variants: tuple[ArgumentVariant, ...] = ()
    subcommand_variants: tuple[ArgumentVariant, ...] = ()
    file_variants: tuple[ArgumentVariant, ...] = ()
    sequence_subcommands: tuple[tuple[str, ...], ...] = ()
    # Names for the sequence steps.  A judge that compares steps by name needs
    # them declared; without this the driver can only number them, and a
    # lookup by name finds nothing.
    sequence_labels: tuple[str, ...] = ()
    sequence_pause_seconds: int = 0
    seed_files: tuple[SeedFile, ...] = ()
    rounds: tuple[ToolRound, ...] = ()
    command: str = ""
    target_file: str = ""
    target_written: str = ""
    target_original: str = ""
    empty_task: bool = False
    extra_argv: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    local_web_probe: bool = False
    git_fixture: bool = False
    execution_mode: HeadlessExecutionMode = HeadlessExecutionMode.STANDARD
    formal_runtime_probes: tuple[FormalProbe, ...] = ()
    formal_contract_probes: tuple[FormalProbe, ...] = ()
    listing_probes: tuple[ListingProbe, ...] = ()
    cli_file_probes: tuple[CliFileProbe, ...] = ()
    status_observation_required: bool = False
    seed_timeout_seconds: int = 0
    measurement_timeout_seconds: int = 0
    timeout_seconds: int = 60
    oracle: HeadlessOracleBinding = HeadlessOracleBinding()

    def __post_init__(self) -> None:
        if type(self.profile) is not HeadlessProfile:
            raise ContractDocumentError("headless profile is invalid")
        if (
            type(self.case_digest) is not str
            or not self.case_digest.startswith("sha256:")
            or len(self.case_digest) != 71
        ):
            raise ContractDocumentError("headless case digest is invalid")
        if type(self.dependencies) is not tuple or any(
            type(v) is not tuple
            or len(v) != 2
            or type(v[0]) is not str
            or type(v[1]) is not DependencyKind
            for v in self.dependencies
        ):
            raise ContractDocumentError("headless dependencies are invalid")
        if any(
            type(v) is not ArgumentVariant
            for v in self.cli_variants + self.subcommand_variants + self.file_variants
        ):
            raise ContractDocumentError("headless recipe variants are invalid")
        if any(
            type(step) is not tuple or any(type(v) is not str for v in step)
            for step in self.sequence_subcommands
        ):
            raise ContractDocumentError("headless sequence is invalid")
        if any(type(v) is not SeedFile for v in self.seed_files) or any(
            type(v) is not ToolRound for v in self.rounds
        ):
            raise ContractDocumentError("headless fixture recipe is invalid")
        if type(self.environment) is not tuple or any(
            type(v) is not tuple or len(v) != 2 or any(type(x) is not str for x in v)
            for v in self.environment
        ):
            raise ContractDocumentError("headless environment is invalid")
        if (
            type(self.timeout_seconds) is not int
            or self.timeout_seconds <= 0
            or type(self.sequence_pause_seconds) is not int
            or self.sequence_pause_seconds < 0
        ):
            raise ContractDocumentError("headless timing is invalid")
        if type(self.oracle) is not HeadlessOracleBinding:
            raise ContractDocumentError("headless oracle binding is invalid")
        if type(self.execution_mode) is not HeadlessExecutionMode:
            raise ContractDocumentError("headless execution mode is invalid")
        for values, expected, name in (
            (self.formal_runtime_probes, FormalProbe, "formal runtime probes"),
            (self.formal_contract_probes, FormalProbe, "formal contract probes"),
            (self.listing_probes, ListingProbe, "listing probes"),
            (self.cli_file_probes, CliFileProbe, "CLI file probes"),
        ):
            if type(values) is not tuple or any(type(v) is not expected for v in values):
                raise ContractDocumentError(f"headless {name} are invalid")
            if len({v.label for v in values}) != len(values):
                raise ContractDocumentError(f"headless {name} labels must be unique")
        if type(self.status_observation_required) is not bool:
            raise ContractDocumentError("headless status observation flag is invalid")
        if any(
            type(value) is not int or value < 0
            for value in (self.seed_timeout_seconds, self.measurement_timeout_seconds)
        ):
            raise ContractDocumentError("headless auxiliary timing is invalid")
        for value in (
            self.task,
            self.output_format,
            self.reply_content,
            self.gateway_mode,
            self.append_system_prompt,
            self.system_prompt,
            self.probe_token,
            self.command,
            self.target_written,
            self.target_original,
        ):
            _text(value, "recipe text")
        _relative_path(self.target_file, "target path")
        _strings(self.probe_tokens, "probe tokens", unique=True)
        _strings(self.extra_argv, "extra argv")
        for flag in (
            self.blocking,
            self.empty_task,
            self.local_web_probe,
            self.git_fixture,
        ):
            if type(flag) is not bool:
                raise ContractDocumentError("headless recipe flag is invalid")
        if len(
            {
                v.label
                for v in self.cli_variants
                + self.subcommand_variants
                + self.file_variants
            }
        ) != len(self.cli_variants + self.subcommand_variants + self.file_variants):
            raise ContractDocumentError("headless recipe variant labels must be unique")


@dataclass(frozen=True, slots=True)
class RequestRecord:
    body: str
    model: str = ""
    system_text: str = ""
    probe_hits: tuple[str, ...] = ()
    system_probe_hits: tuple[str, ...] = ()
    tool_names: tuple[str, ...] = ()
    tool_result_texts: tuple[str, ...] = ()
    stream_enabled: bool | None = None

    def __post_init__(self) -> None:
        _text(self.body, "request body")
        _text(self.model, "request model")
        _text(self.system_text, "request system text")
        _strings(self.probe_hits, "request probe hits", unique=True)
        _strings(self.system_probe_hits, "request system probe hits", unique=True)
        _strings(self.tool_names, "request tool names", unique=True)
        _strings(self.tool_result_texts, "request tool results")
        if self.stream_enabled is not None and type(self.stream_enabled) is not bool:
            raise ContractDocumentError("headless request stream flag is invalid")


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    """One directly observed CLI process, variant, or sequence step."""

    label: str
    rc: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    requests: tuple[RequestRecord, ...] = ()
    written_paths: tuple[str, ...] = ()
    removed_paths: tuple[str, ...] = ()
    target_content: str = ""
    target_found_at: str = ""
    worktree_target_content: str = ""
    worktree_paths: tuple[str, ...] = ()
    git_initialized: bool = False
    marker_found_at: str = ""
    web_gets: tuple[str, ...] = ()
    exit_status: str = ""
    start_error: str = ""
    cleanup_complete: bool = True
    out_created: bool = False
    repo_changed: bool = False
    preterminal_stdout: str = ""
    barrier_seen: bool | None = None
    gateway_wait_timed_out: bool | None = None
    terminal_before_release: bool | None = None
    first_stdout_ns: int | None = None
    release_ns: int | None = None
    count: int | None = None
    payload_bytes: int | None = None
    seed_created: int | None = None
    listed_markers: int | None = None
    rchar_delta: int | None = None
    rss_delta_kib: int | None = None
    home_open_count: int | None = None
    output_bytes: int | None = None
    product_error: str = ""

    def __post_init__(self) -> None:
        if type(self.label) is not str or not self.label:
            raise ContractDocumentError("headless process label is invalid")
        if self.rc is not None and type(self.rc) is not int:
            raise ContractDocumentError("headless process rc is invalid")
        if type(self.timed_out) is not bool or any(
            type(v) is not RequestRecord for v in self.requests
        ):
            raise ContractDocumentError("headless process evidence is invalid")
        if type(self.requests) is not tuple or len(self.requests) > _MAX_ITEMS:
            raise ContractDocumentError("headless process requests are invalid")
        for value in (
            self.stdout,
            self.stderr,
            self.target_content,
            self.target_found_at,
            self.worktree_target_content,
            self.marker_found_at,
            self.exit_status,
            self.start_error,
            self.preterminal_stdout,
            self.product_error,
        ):
            _text(value, "process text")
        _strings(self.written_paths, "written paths", unique=True)
        _strings(self.removed_paths, "removed paths", unique=True)
        _strings(self.worktree_paths, "worktree paths", unique=True)
        _strings(self.web_gets, "web gets")
        for path in self.written_paths + self.removed_paths:
            _relative_path(path, "filesystem path", allow_empty=False)
        if type(self.git_initialized) is not bool:
            raise ContractDocumentError("headless git observation is invalid")
        if any(
            type(value) is not bool
            for value in (self.cleanup_complete, self.out_created, self.repo_changed)
        ):
            raise ContractDocumentError("headless process fact flags are invalid")
        for value in (
            self.barrier_seen,
            self.gateway_wait_timed_out,
            self.terminal_before_release,
        ):
            if value is not None and type(value) is not bool:
                raise ContractDocumentError("headless timeline flags are invalid")
        for value in (
            self.first_stdout_ns,
            self.release_ns,
            self.count,
            self.payload_bytes,
            self.seed_created,
            self.listed_markers,
            self.rchar_delta,
            self.rss_delta_kib,
            self.home_open_count,
            self.output_bytes,
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ContractDocumentError("headless numeric process fact is invalid")


@dataclass(frozen=True, slots=True)
class ObservedFile:
    path: str
    content: str

    def __post_init__(self) -> None:
        _relative_path(self.path, "observed file path", allow_empty=False)
        _text(self.content, "observed file content")


@dataclass(frozen=True, slots=True)
class HeadlessScenario:
    definition: ScenarioDefinition
    recipe: HeadlessRecipe

    def __post_init__(self) -> None:
        if (
            type(self.definition) is not ScenarioDefinition
            or type(self.recipe) is not HeadlessRecipe
        ):
            raise ContractDocumentError("headless scenario requires exact typed values")
        if self.definition.classification.primary != "headless":
            raise ContractDocumentError(
                "headless scenario owner differs from its Harness"
            )


@dataclass(frozen=True, slots=True)
class HeadlessObservation:
    scenario_id: str
    profile: HeadlessProfile
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    backend_evidence_digest: str
    cli_available: bool
    runs: tuple[ProcessObservation, ...]
    artifact_files: tuple[ObservedFile, ...] = ()
    repository_root: str = ""
    agent_session_id: str = ""
    driver_error: str = ""
    process_tree_clean: bool = True
    workspace_clean: bool = True
    checkpoint_id: str = ""
    pre_listing: str = ""
    selection_command: str = ""
    selection_result: str = ""
    summary_sent: bool | None = None
    session_start_error: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.scenario_id) is not str
            or _ID.fullmatch(self.scenario_id) is None
            or type(self.profile) is not HeadlessProfile
        ):
            raise ContractDocumentError("headless observation identity is invalid")
        for value in (
            self.plan_digest,
            self.artifact_digest,
            self.backend_evidence_digest,
        ):
            if (
                type(value) is not str
                or not value.startswith("sha256:")
                or len(value) != 71
            ):
                raise ContractDocumentError("headless evidence digest is invalid")
        if type(self.artifact_id) is not str or not self.artifact_id:
            raise ContractDocumentError("headless artifact id is invalid")
        if type(self.runs) is not tuple or any(
            type(v) is not ProcessObservation for v in self.runs
        ):
            raise ContractDocumentError("headless raw runs are invalid")
        if any(type(v) is not ObservedFile for v in self.artifact_files):
            raise ContractDocumentError("headless artifact files are invalid")
        if any(
            type(v) is not bool
            for v in (self.cli_available, self.process_tree_clean, self.workspace_clean)
        ):
            raise ContractDocumentError("headless observation flags are invalid")
        for value in (
            self.repository_root,
            self.agent_session_id,
            self.driver_error,
            self.checkpoint_id,
            self.pre_listing,
            self.selection_command,
            self.selection_result,
            self.session_start_error,
        ):
            _text(value, "observation text")
        if self.summary_sent is not None and type(self.summary_sent) is not bool:
            raise ContractDocumentError("headless compact summary fact is invalid")
        labels = tuple(run.label for run in self.runs)
        if len(set(labels)) != len(labels):
            raise ContractDocumentError("headless run labels must be unique")
        paths = tuple(item.path for item in self.artifact_files)
        if len(set(paths)) != len(paths):
            raise ContractDocumentError("headless artifact file paths must be unique")


def define_headless_scenario(
    scenario_id: str, recipe: HeadlessRecipe, role: ScenarioRole, /
) -> HeadlessScenario:
    dependencies = tuple(
        sorted(
            (DependencyRef(test_id, kind) for test_id, kind in recipe.dependencies),
            key=lambda item: (item.scenario_id, item.kind.value),
        )
    )
    definition = ScenarioDefinition(
        scenario_id,
        "1",
        Classification("headless"),
        role,
        1_000_000 if role is ScenarioRole.TARGET else 0,
        ScenarioDifficulty.HARD,
        dependencies,
        ExperimentBinding(
            ComponentRef("headless-fixture", "1", ComponentKind.FIXTURE),
            ComponentRef("headless-adapter", "1", ComponentKind.STIMULUS),
            ComponentRef("headless-observer", "1", ComponentKind.OBSERVER),
            ComponentRef("headless-oracle", "1", ComponentKind.ORACLE),
            ComponentRef("headless-cleanup", "1", ComponentKind.CLEANUP),
        ),
        UnsupportedPolicy.FAIL,
    )
    return HeadlessScenario(definition, recipe)


def evaluation_digest(
    scenario: HeadlessScenario, observation: HeadlessObservation
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
            b"doc2harness.headless-evaluation/1\0" + canonical_json(body)
        ).hexdigest()
    )


def recipe_digest(recipe: HeadlessRecipe, /) -> str:
    """Bind every typed stimulus and checker closure value into the plan."""

    if type(recipe) is not HeadlessRecipe:
        raise ContractDocumentError("headless recipe digest requires an exact recipe")
    body = asdict(recipe)
    extension_keys = (
        "execution_mode",
        "formal_runtime_probes",
        "formal_contract_probes",
        "listing_probes",
        "cli_file_probes",
        "status_observation_required",
        "seed_timeout_seconds",
        "measurement_timeout_seconds",
    )
    if (
        recipe.execution_mode is HeadlessExecutionMode.STANDARD
        and not recipe.formal_runtime_probes
        and not recipe.formal_contract_probes
        and not recipe.listing_probes
        and not recipe.cli_file_probes
        and not recipe.status_observation_required
        and recipe.seed_timeout_seconds == 0
        and recipe.measurement_timeout_seconds == 0
    ):
        for key in extension_keys:
            body.pop(key)
    return (
        "sha256:"
        + hashlib.sha256(
            b"doc2harness.headless-recipe/1\0" + canonical_json(body)
        ).hexdigest()
    )
