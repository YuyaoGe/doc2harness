"""Closed declarations and observations for direct mock-process scenarios."""

from __future__ import annotations
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
)
from doc2harness.contracts.primitives import ContractDocumentError

_SCENARIO_ID = re.compile("[a-z0-9][a-z0-9._-]{0,127}\\Z", re.ASCII)
_MAX_STEPS = 256
_MAX_TIMEOUT_SECONDS = 900


class DirectMockMode(str, Enum):
    """Closed gateway response families for direct-mock scenarios."""

    NORMAL = "NORMAL"
    SEQUENTIAL_PROBE = "SEQUENTIAL_PROBE"
    FIXED_REPLY = "FIXED_REPLY"


class DirectMockEntrypoint(str, Enum):
    """Public process surfaces supported by the direct-mock backend."""

    PYTHON_ENTRY = "PYTHON_ENTRY"
    CONTRACT_SCRIPT = "CONTRACT_SCRIPT"
    AGENT_API_DRIVER = "AGENT_API_DRIVER"


class DirectMockRequiredFlag(str, Enum):
    TASK_FILE = "--task-file"
    REPO_DIR = "--repo-dir"
    OUT_DIR = "--out-dir"
    BUDGET_FILE = "--budget-file"
    MODEL_BASE_URL = "--model-base-url"
    MODEL_NAME = "--model-name"
    MODEL_API_KEY = "--model-api-key"


class DirectMockFixtureProfile(str, Enum):
    """Named fixture intents; ``fixtures.py`` materializes their bytes."""

    NONE = "NONE"
    QUALITY_GATE_FAIL = "QUALITY_GATE_FAIL"
    QUALITY_GATE_PASS = "QUALITY_GATE_PASS"
    SECRET_UNREFERENCED_KEY = "SECRET_UNREFERENCED_KEY"
    SECRET_CONTROL = "SECRET_CONTROL"
    SECRET_PEM = "SECRET_PEM"
    WEB_FETCH = "WEB_FETCH"
    HTTP_PROXY = "HTTP_PROXY"
    SHELL_EDGE_CONTROL = "SHELL_EDGE_CONTROL"
    SHELL_EDGE_TARGET = "SHELL_EDGE_TARGET"


class DirectMockOracleProfile(str, Enum):
    """Closed checker families; every scenario declares one explicitly."""

    UNDECLARED = "UNDECLARED"
    QUALITY_GATE_BLOCKS = "QUALITY_GATE_BLOCKS"
    QUALITY_GATE_ALLOWS_CONTROL = "QUALITY_GATE_ALLOWS_CONTROL"
    PROBE_ABSENT = "PROBE_ABSENT"
    PROBE_PRESENT = "PROBE_PRESENT"
    FACT_CONTRACT = "FACT_CONTRACT"


class DirectMockFactKey(str, Enum):
    """Bounded backend facts needed by frozen direct-mock oracles."""

    PARALLEL_A = "PARALLEL_A"
    PARALLEL_B = "PARALLEL_B"
    QGATE_RAN = "QGATE_RAN"
    DENY_MARKER_PRESENT = "DENY_MARKER_PRESENT"
    EXPECTED_REPOSITORY_FILE = "EXPECTED_REPOSITORY_FILE"
    REPOSITORY_CLEAN = "REPOSITORY_CLEAN"
    SHARED_FILE_PRESENT = "SHARED_FILE_PRESENT"
    SHARED_FILE_MARKER = "SHARED_FILE_MARKER"
    SHARED_FILE_CONTENT = "SHARED_FILE_CONTENT"
    SUBAGENT_OBSERVED = "SUBAGENT_OBSERVED"
    OFFLINE_LOOPBACK_ONLY = "OFFLINE_LOOPBACK_ONLY"
    PARENT_PROFILE_HEALTHY = "PARENT_PROFILE_HEALTHY"
    PROFILE_INTERSECTION_HELD = "PROFILE_INTERSECTION_HELD"
    ACTIVE_TURN_COMMAND_SUPPORTED = "ACTIVE_TURN_COMMAND_SUPPORTED"
    ACTIVE_TURN_INFLIGHT = "ACTIVE_TURN_INFLIGHT"
    WEB_FETCH_HEALTHY = "WEB_FETCH_HEALTHY"
    WEB_FAILURES_PRESERVED = "WEB_FAILURES_PRESERVED"
    PROXY_SELF_CHECKED = "PROXY_SELF_CHECKED"
    EXPLICIT_ENDPOINT_BYPASSED_PROXY = "EXPLICIT_ENDPOINT_BYPASSED_PROXY"
    SHELL_CONTROL_BYTES = "SHELL_CONTROL_BYTES"
    SHELL_EDGE_BYTES = "SHELL_EDGE_BYTES"
    RESPONSE_USAGE_HEALTHY = "RESPONSE_USAGE_HEALTHY"
    CHILD_TOOLS_ALLOWLIST = "CHILD_TOOLS_ALLOWLIST"
    REASONING_REDACTED = "REASONING_REDACTED"
    SINK_FAULT_APPLIED = "SINK_FAULT_APPLIED"
    SESSION_SINK_TOKEN_SEEN = "SESSION_SINK_TOKEN_SEEN"
    SINK_ERROR_SURFACED = "SINK_ERROR_SURFACED"


class DirectMockIsolationProfile(str, Enum):
    """Workspace isolation required by a recipe."""

    STANDARD = "STANDARD"
    DECOY_CWD_WITH_ARTIFACT_GUARD = "DECOY_CWD_WITH_ARTIFACT_GUARD"


class DirectMockOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class DirectMockApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class DirectMockRecipe:
    """Bounded recipe metadata without commands, fixture bytes, secrets or paths."""

    mode: DirectMockMode
    entrypoint: DirectMockEntrypoint
    max_steps: int | None = None
    timeout_seconds: int | None = None
    omitted_flag: DirectMockRequiredFlag | None = None
    fixture_profile: DirectMockFixtureProfile = DirectMockFixtureProfile.NONE
    isolation_profile: DirectMockIsolationProfile = DirectMockIsolationProfile.STANDARD
    oracle_profile: DirectMockOracleProfile = DirectMockOracleProfile.UNDECLARED
    command: str | None = None
    environment: tuple[tuple[str, str], ...] = ()
    expected_token: str | None = None
    base_url_suffix: str = ""
    model_name: str = "litellm_proxy/mock"
    reject_substring: str = ""
    allow_stderr_traceback: bool = False
    stale_pwd_probe: bool = False
    initialize_git: bool = False
    control_dependency: str | None = None
    control_dependencies: tuple[str, ...] = ()
    requirement_dependencies: tuple[str, ...] = ()
    tool_family: tuple[str, ...] = ()
    required_tool_families: tuple[tuple[str, ...], ...] = ()
    request_limit: int | None = None
    body_limit: int | None = None
    required_facts: tuple[DirectMockFactKey, ...] = ()
    variant_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.mode) is not DirectMockMode:
            raise ContractDocumentError("direct mock mode must use the closed vocabulary")
        if type(self.entrypoint) is not DirectMockEntrypoint:
            raise ContractDocumentError("direct mock entrypoint must use the closed vocabulary")
        if self.max_steps is not None and (
            type(self.max_steps) is not int or self.max_steps < 1 or self.max_steps > _MAX_STEPS
        ):
            raise ContractDocumentError("direct mock max steps are invalid")
        if self.timeout_seconds is not None and (
            type(self.timeout_seconds) is not int
            or self.timeout_seconds < 1
            or self.timeout_seconds > _MAX_TIMEOUT_SECONDS
        ):
            raise ContractDocumentError("direct mock timeout is invalid")
        if self.omitted_flag is not None and type(self.omitted_flag) is not DirectMockRequiredFlag:
            raise ContractDocumentError("direct mock omitted flag must use the closed vocabulary")
        if type(self.fixture_profile) is not DirectMockFixtureProfile:
            raise ContractDocumentError("direct mock fixture profile must use the closed vocabulary")
        if type(self.isolation_profile) is not DirectMockIsolationProfile:
            raise ContractDocumentError("direct mock isolation profile must use the closed vocabulary")
        if type(self.oracle_profile) is not DirectMockOracleProfile:
            raise ContractDocumentError("direct mock oracle profile must use the closed vocabulary")
        if self.command is not None and (
            type(self.command) is not str or not self.command or len(self.command) > 16384
        ):
            raise ContractDocumentError("direct mock command is invalid")
        if type(self.environment) is not tuple or any(
            (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or (type(item[1]) is not str)
                or (not item[0])
                or (len(item[0]) > 128)
                or (len(item[1]) > 16384)
                for item in self.environment
            )
        ):
            raise ContractDocumentError("direct mock environment is invalid")
        if len({item[0] for item in self.environment}) != len(self.environment):
            raise ContractDocumentError("direct mock environment keys must be unique")
        if self.expected_token is not None and (
            type(self.expected_token) is not str or not self.expected_token or len(self.expected_token) > 1024
        ):
            raise ContractDocumentError("direct mock expected token is invalid")
        if (
            type(self.base_url_suffix) is not str
            or len(self.base_url_suffix) > 1024
            or ".." in self.base_url_suffix
        ):
            raise ContractDocumentError("direct mock base-url suffix is invalid")
        if type(self.model_name) is not str or not self.model_name or len(self.model_name) > 512:
            raise ContractDocumentError("direct mock model name is invalid")
        if type(self.reject_substring) is not str or len(self.reject_substring) > 1024:
            raise ContractDocumentError("direct mock rejection substring is invalid")
        if type(self.allow_stderr_traceback) is not bool:
            raise ContractDocumentError("direct mock traceback policy is invalid")
        if type(self.stale_pwd_probe) is not bool or type(self.initialize_git) is not bool:
            raise ContractDocumentError("direct mock repository setup policy is invalid")
        if self.control_dependency is not None and (
            type(self.control_dependency) is not str
            or _SCENARIO_ID.fullmatch(self.control_dependency) is None
        ):
            raise ContractDocumentError("direct mock control dependency is invalid")
        if (
            type(self.control_dependencies) is not tuple
            or any(
                (
                    type(item) is not str or _SCENARIO_ID.fullmatch(item) is None
                    for item in self.control_dependencies
                )
            )
            or len(set(self.control_dependencies)) != len(self.control_dependencies)
        ):
            raise ContractDocumentError("direct mock control dependencies are invalid")
        if self.control_dependency is not None and self.control_dependency in self.control_dependencies:
            raise ContractDocumentError("direct mock control dependencies must be unique")
        if (
            type(self.requirement_dependencies) is not tuple
            or any(
                (
                    type(item) is not str or _SCENARIO_ID.fullmatch(item) is None
                    for item in self.requirement_dependencies
                )
            )
            or len(set(self.requirement_dependencies)) != len(self.requirement_dependencies)
        ):
            raise ContractDocumentError("direct mock requirement dependencies are invalid")
        if type(self.tool_family) is not tuple or any(
            (type(item) is not str or not item or len(item) > 128 for item in self.tool_family)
        ):
            raise ContractDocumentError("direct mock tool family is invalid")
        if (
            type(self.required_tool_families) is not tuple
            or any(
                (
                    type(family) is not tuple
                    or not family
                    or any((type(item) is not str or not item or len(item) > 128 for item in family))
                    or (len(set(family)) != len(family))
                    for family in self.required_tool_families
                )
            )
            or len(set(self.required_tool_families)) != len(self.required_tool_families)
        ):
            raise ContractDocumentError("direct mock required tool families are invalid")
        if self.request_limit is not None and (
            type(self.request_limit) is not int or self.request_limit < 1 or self.request_limit > 4096
        ):
            raise ContractDocumentError("direct mock request limit is invalid")
        if self.body_limit is not None and (
            type(self.body_limit) is not int or self.body_limit < 1 or self.body_limit > 64 * 1024 * 1024
        ):
            raise ContractDocumentError("direct mock body limit is invalid")
        if (
            type(self.required_facts) is not tuple
            or any((type(item) is not DirectMockFactKey for item in self.required_facts))
            or len(set(self.required_facts)) != len(self.required_facts)
        ):
            raise ContractDocumentError("direct mock required facts are invalid")
        if (
            type(self.variant_labels) is not tuple
            or any(
                (
                    type(item) is not str or _SCENARIO_ID.fullmatch(item) is None
                    for item in self.variant_labels
                )
            )
            or len(set(self.variant_labels)) != len(self.variant_labels)
        ):
            raise ContractDocumentError("direct mock variant labels are invalid")
        if self.omitted_flag is not None and (
            self.entrypoint is not DirectMockEntrypoint.CONTRACT_SCRIPT
            or self.mode is not DirectMockMode.NORMAL
        ):
            raise ContractDocumentError("required flags can only be omitted at the contract entrypoint")
        decoy = self.isolation_profile is DirectMockIsolationProfile.DECOY_CWD_WITH_ARTIFACT_GUARD
        if decoy != (self.omitted_flag is DirectMockRequiredFlag.REPO_DIR):
            raise ContractDocumentError("repo-dir omission requires the decoy artifact guard")


@dataclass(frozen=True, slots=True)
class DirectMockFactStep:
    """Typed observation fact consumed by a local judge."""

    fact_key: DirectMockFactKey


def capture_fact(*, fact_key: DirectMockFactKey) -> DirectMockFactStep:
    """Declare a typed fact that the scenario judge will consume."""
    return DirectMockFactStep(fact_key)


@dataclass(frozen=True, slots=True)
class DirectMockScenario:
    """One independent benchmark definition paired with its recipe declaration."""

    definition: ScenarioDefinition
    recipe: DirectMockRecipe

    def __post_init__(self) -> None:
        if type(self.definition) is not ScenarioDefinition or type(self.recipe) is not DirectMockRecipe:
            raise ContractDocumentError("direct mock scenario requires exact typed values")
        if self.definition.classification.primary != "direct":
            raise ContractDocumentError("direct mock scenario owner differs from its Harness layer")


@dataclass(frozen=True, slots=True)
class DirectMockRequestFact:
    """Bounded request facts retained by the gateway; request content is discarded."""

    body_bytes: int
    has_tools: bool
    tool_names: tuple[str, ...]
    probe_seen: bool
    message_count: int
    message_roles: tuple[str, ...]
    path: str = ""
    model: str = ""
    nonleading_system: bool = False
    orphan_tool_result: bool = False
    lone_surrogate: bool = False
    has_any_null: bool = False
    empty_content_block: bool = False
    dangling_tool_use: bool = False
    trailing_empty_assistant: bool = False
    tool_result_max_bytes: int = 0
    target_tool_result: str = ""
    has_reasoning: bool = False

    def __post_init__(self) -> None:
        if type(self.body_bytes) is not int or self.body_bytes < 0 or self.body_bytes > 64 * 1024 * 1024:
            raise ContractDocumentError("direct mock request size is invalid")
        if type(self.has_tools) is not bool:
            raise ContractDocumentError("direct mock tool-presence fact is invalid")
        if type(self.tool_names) is not tuple or len(self.tool_names) > 256:
            raise ContractDocumentError("direct mock tool-name facts are invalid")
        if any((type(item) is not str or not item or len(item) > 256 for item in self.tool_names)):
            raise ContractDocumentError("direct mock tool-name fact is invalid")
        if type(self.probe_seen) is not bool:
            raise ContractDocumentError("direct mock probe fact is invalid")
        if type(self.message_count) is not int or self.message_count < 0 or self.message_count > 4096:
            raise ContractDocumentError("direct mock message count is invalid")
        if type(self.message_roles) is not tuple or len(self.message_roles) > 32:
            raise ContractDocumentError("direct mock role facts are invalid")
        if (
            type(self.path) is not str
            or len(self.path) > 2048
            or type(self.model) is not str
            or (len(self.model) > 512)
        ):
            raise ContractDocumentError("direct mock request routing facts are invalid")
        for value in (
            self.nonleading_system,
            self.orphan_tool_result,
            self.lone_surrogate,
            self.has_any_null,
            self.empty_content_block,
            self.dangling_tool_use,
            self.trailing_empty_assistant,
            self.has_reasoning,
        ):
            if type(value) is not bool:
                raise ContractDocumentError("direct mock request wire fact is invalid")
        if (
            type(self.tool_result_max_bytes) is not int
            or not 0 <= self.tool_result_max_bytes <= 64 * 1024 * 1024
        ):
            raise ContractDocumentError("direct mock tool-result size is invalid")
        if type(self.target_tool_result) is not str or len(self.target_tool_result) > 1024 * 1024:
            raise ContractDocumentError("direct mock target result is invalid")


@dataclass(frozen=True, slots=True)
class DirectMockObservedFact:
    """One closed, bounded filesystem or lifecycle fact emitted by the backend."""

    key: DirectMockFactKey
    value: bool | int | str

    def __post_init__(self) -> None:
        if type(self.key) is not DirectMockFactKey or type(self.value) not in {bool, int, str}:
            raise ContractDocumentError("direct mock observed fact is invalid")
        if type(self.value) is str and len(self.value) > 8 * 1024 * 1024:
            raise ContractDocumentError("direct mock observed text is too large")


def _digest(value: str, label: str) -> str:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any((character not in "0123456789abcdef" for character in value[7:]))
    ):
        raise ContractDocumentError(f"{label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DirectMockRawObservation:
    """Path-free execution facts emitted by an identity-bound backend."""

    scenario_id: str
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    exit_code: int
    status_written: bool
    exit_status: str | None
    cost_usd: int | float | None
    requests: tuple[DirectMockRequestFact, ...]
    proof_present: bool
    proof_contains_marker: bool
    trajectory_observed: bool
    trajectory_contains_probe: bool
    artifact_unchanged: bool
    decoy_entries: tuple[str, ...]
    stderr_has_traceback: bool
    process_tree_clean: bool
    gateway_clean: bool
    workspace_clean: bool
    backend_evidence_digest: str
    timed_out: bool = False
    trajectory: str = ""
    assistant_text: str = ""
    facts: tuple[DirectMockObservedFact, ...] = ()

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _SCENARIO_ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("direct mock observation id is invalid")
        _digest(self.plan_digest, "direct mock plan digest")
        if type(self.artifact_id) is not str or _SCENARIO_ID.fullmatch(self.artifact_id) is None:
            raise ContractDocumentError("direct mock artifact id is invalid")
        _digest(self.artifact_digest, "direct mock artifact digest")
        if type(self.exit_code) is not int or self.exit_code < -1 or self.exit_code > 255:
            raise ContractDocumentError("direct mock exit code is invalid")
        if self.exit_status is not None and (
            type(self.exit_status) is not str or len(self.exit_status) > 256
        ):
            raise ContractDocumentError("direct mock exit status is invalid")
        if self.cost_usd is not None and type(self.cost_usd) not in {int, float}:
            raise ContractDocumentError("direct mock cost fact is invalid")
        if type(self.requests) is not tuple or any(
            (type(item) is not DirectMockRequestFact for item in self.requests)
        ):
            raise ContractDocumentError("direct mock request facts are invalid")
        for value in (
            self.status_written,
            self.proof_present,
            self.proof_contains_marker,
            self.trajectory_observed,
            self.trajectory_contains_probe,
            self.artifact_unchanged,
            self.stderr_has_traceback,
            self.process_tree_clean,
            self.gateway_clean,
            self.workspace_clean,
        ):
            if type(value) is not bool:
                raise ContractDocumentError("direct mock boolean fact is invalid")
        if type(self.decoy_entries) is not tuple or any(
            (type(item) is not str or not item or len(item) > 512 for item in self.decoy_entries)
        ):
            raise ContractDocumentError("direct mock decoy facts are invalid")
        _digest(self.backend_evidence_digest, "direct mock backend evidence digest")
        if type(self.timed_out) is not bool:
            raise ContractDocumentError("direct mock timeout fact is invalid")
        if type(self.trajectory) is not str or len(self.trajectory) > 64 * 1024 * 1024:
            raise ContractDocumentError("direct mock trajectory is invalid")
        if type(self.assistant_text) is not str or len(self.assistant_text) > 8 * 1024 * 1024:
            raise ContractDocumentError("direct mock assistant text is invalid")
        if type(self.facts) is not tuple or any(
            (type(item) is not DirectMockObservedFact for item in self.facts)
        ):
            raise ContractDocumentError("direct mock observed facts are invalid")
        if len({item.key for item in self.facts}) != len(self.facts):
            raise ContractDocumentError("direct mock observed facts must have unique keys")


@dataclass(frozen=True, slots=True)
class DirectMockJudgement:
    """Typed, scenario-local oracle result before evidence/score construction."""

    outcome: DirectMockOutcome
    applicability: DirectMockApplicability
    reason_code: str

    def __post_init__(self) -> None:
        if (
            type(self.outcome) is not DirectMockOutcome
            or type(self.applicability) is not DirectMockApplicability
        ):
            raise ContractDocumentError("direct mock judgement uses invalid closed values")
        if type(self.reason_code) is not str or not self.reason_code or len(self.reason_code) > 128:
            raise ContractDocumentError("direct mock judgement reason is invalid")


@runtime_checkable
class DirectMockLocalJudge(Protocol):
    """Pure scenario-specific predicate over an already bound observation."""

    def __call__(self, observation: DirectMockRawObservation, /) -> DirectMockJudgement: ...


def define_direct_mock_scenario(
    scenario_id: str, recipe: DirectMockRecipe, role: ScenarioRole, /
) -> DirectMockScenario:
    """Construct one direct-mock definition without granting execution authority."""
    if type(scenario_id) is not str or _SCENARIO_ID.fullmatch(scenario_id) is None:
        raise ContractDocumentError("direct mock scenario id is not canonical")
    if type(recipe) is not DirectMockRecipe:
        raise ContractDocumentError("direct mock scenario recipe must use DirectMockRecipe")
    if type(role) is not ScenarioRole:
        raise ContractDocumentError("direct mock scenario role must use ScenarioRole")
    control_ids = (
        () if recipe.control_dependency is None else (recipe.control_dependency,)
    ) + recipe.control_dependencies
    definition = ScenarioDefinition(
        scenario_id=scenario_id,
        revision="1",
        classification=Classification("direct"),
        role=role,
        weight_microunits=1000000 if role is ScenarioRole.TARGET else 0,
        difficulty=ScenarioDifficulty.STANDARD,
        dependencies=(
            *(DependencyRef(item, DependencyKind.GATE) for item in control_ids),
            *(DependencyRef(item, DependencyKind.REQUIREMENT) for item in recipe.requirement_dependencies),
        ),
        experiment=ExperimentBinding(
            fixture=ComponentRef("direct-mock-fixture", "1", ComponentKind.FIXTURE),
            stimulus=ComponentRef("direct-mock-process", "1", ComponentKind.STIMULUS),
            observer=ComponentRef("direct-mock-observer", "1", ComponentKind.OBSERVER),
            oracle=ComponentRef("direct-mock-oracle", "1", ComponentKind.ORACLE),
            cleanup=ComponentRef("direct-mock-cleanup", "1", ComponentKind.CLEANUP),
        ),
        unsupported_policy=UnsupportedPolicy.FAIL,
    )
    return DirectMockScenario(definition, recipe)


def validate_direct_mock_observation(
    scenario: DirectMockScenario,
    observation: DirectMockRawObservation,
    expected_plan_digest: str | None = None,
    /,
) -> None:
    """Validate the identity and plan binding before a local judge runs.

    Artifact identity and the runtime-generated plan digest are verified by
    ``RuntimeDirectMockAdapter``.  A caller that already has an expected plan
    digest may pass it explicitly; keeping the default path free of an
    adapter import preserves the one-way harness dependency graph.
    """
    if type(scenario) is not DirectMockScenario or type(observation) is not DirectMockRawObservation:
        raise ContractDocumentError("direct mock observation binding requires exact contracts")
    if observation.scenario_id != scenario.definition.scenario_id:
        raise ContractDocumentError("direct mock observation belongs to another scenario")
    if expected_plan_digest is not None and observation.plan_digest != expected_plan_digest:
        raise ContractDocumentError("direct mock observation plan is not bound to the scenario")


def _validate_local_judgement(judgement: DirectMockJudgement) -> DirectMockJudgement:
    if type(judgement) is not DirectMockJudgement:
        raise ContractDocumentError("direct mock local judge returned an invalid judgement")
    return judgement


def apply_direct_mock_local_judge(
    scenario: DirectMockScenario, observation: DirectMockRawObservation, judge: DirectMockLocalJudge, /
) -> DirectMockJudgement:
    """Apply shared runtime gates, then one scenario-local pure judge.

    A stderr traceback fails the run outright; incomplete cleanup fails it unless
    the scenario's own judgement is already inconclusive.
    """
    validate_direct_mock_observation(scenario, observation)
    if not isinstance(judge, DirectMockLocalJudge):
        raise ContractDocumentError("direct mock local judge is not callable")
    cleanup_complete = (
        observation.process_tree_clean and observation.gateway_clean and observation.workspace_clean
    )
    if observation.stderr_has_traceback and (not scenario.recipe.allow_stderr_traceback):
        return DirectMockJudgement(
            DirectMockOutcome.FAIL, DirectMockApplicability.APPLICABLE, "STDERR_TRACEBACK"
        )
    provisional = _validate_local_judgement(judge(observation))
    if not cleanup_complete:
        if provisional.applicability is DirectMockApplicability.INCONCLUSIVE:
            return provisional
        return DirectMockJudgement(
            DirectMockOutcome.FAIL, DirectMockApplicability.APPLICABLE, "RUNTIME_CLEANUP_INCOMPLETE"
        )
    return provisional


__all__ = [
    "DirectMockApplicability",
    "DirectMockEntrypoint",
    "DirectMockFixtureProfile",
    "DirectMockIsolationProfile",
    "DirectMockJudgement",
    "DirectMockLocalJudge",
    "DirectMockMode",
    "DirectMockOutcome",
    "DirectMockRawObservation",
    "DirectMockRecipe",
    "DirectMockRequestFact",
    "DirectMockRequiredFlag",
    "DirectMockScenario",
    "apply_direct_mock_local_judge",
    "define_direct_mock_scenario",
    "validate_direct_mock_observation",
]
