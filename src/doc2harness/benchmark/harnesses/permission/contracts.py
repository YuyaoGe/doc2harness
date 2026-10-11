"""Closed declarations and observations for child-process permission scenarios."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
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
_ORIGINAL = "ORIG_PERM_0001\n"


class PermissionOracleProfile(str, Enum):
    DENY_BLOCKED = "DENY_BLOCKED"
    COMMAND_EXECUTED = "COMMAND_EXECUTED"
    TARGET_UNCHANGED = "TARGET_UNCHANGED"
    TARGET_MUTATED = "TARGET_MUTATED"
    WRITE_BLOCKED_READ_ALLOWED = "WRITE_BLOCKED_READ_ALLOWED"
    WRITE_AND_READ_EXECUTED = "WRITE_AND_READ_EXECUTED"
    MONITOR_EXECUTED_CONTROL = "MONITOR_EXECUTED_CONTROL"
    MONITOR_BLOCKED = "MONITOR_BLOCKED"
    WIRE_TOKENS_OBSERVED = "WIRE_TOKENS_OBSERVED"
    WIRE_TOKENS_FENCED = "WIRE_TOKENS_FENCED"
    HEALTHY_HOOK_OPERATION_LANDED = "HEALTHY_HOOK_OPERATION_LANDED"
    HOOK_FAILURE_OPERATION_BLOCKED = "HOOK_FAILURE_OPERATION_BLOCKED"


class PermissionOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class PermissionApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


def _string_tuple(value: object, label: str, maximum: int = 256) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or len(value) > maximum
        or any(
            type(item) is not str or not item or len(item) > 16_384 for item in value
        )
    ):
        raise ContractDocumentError(f"permission {label} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class PermissionStep:
    """One closed, author-facing step in a permission probe."""

    operation: str
    value: object

    def __post_init__(self) -> None:
        if self.operation not in {
            "set_mode",
            "add_deny_rule",
            "seed_file",
            "allow_command",
            "probe_command",
            "probe_tool",
            "capture_target_file",
        }:
            raise ContractDocumentError("permission step operation is invalid")


def set_mode(command: str) -> PermissionStep:
    return PermissionStep("set_mode", command)


def add_deny_rule(rule: str) -> PermissionStep:
    return PermissionStep("add_deny_rule", rule)


def seed_file(path: str, content: str) -> PermissionStep:
    return PermissionStep("seed_file", (path, content))


def allow_command(command: str) -> PermissionStep:
    return PermissionStep("allow_command", command)


def probe_command(command: str) -> PermissionStep:
    return PermissionStep("probe_command", command)


def probe_tool(tool: str) -> PermissionStep:
    return PermissionStep("probe_tool", tool)


def capture_target_file(path: str, *, present: bool) -> PermissionStep:
    return PermissionStep("capture_target_file", (path, present))


@dataclass(frozen=True, slots=True)
class PermissionRecipe:
    """Path-independent declaration of one permission experiment."""

    deny_rules: tuple[str, ...]
    setup_commands: tuple[str, ...]
    seed_files: tuple[tuple[str, str], ...]
    allow_command: str
    probe_command: str
    oracle_profile: PermissionOracleProfile
    second_probe: str | None = None
    probe_tool: str = "bash"
    probe_arguments: tuple[tuple[str, str], ...] = ()
    expected_token: str | None = None
    initialize_git: bool = False
    git_baseline_files: tuple[tuple[str, str, str], ...] = ()
    control_dependency: str | None = None
    dependency_kind: DependencyKind = DependencyKind.GATE
    variant_command: str | None = None
    counterfactual_setup_commands: tuple[str, ...] = ()
    mcp_marker: bool = False
    settle_paths: tuple[str, ...] = ()
    probe_tokens: tuple[str, ...] = ()
    custom_read_external: bool = False
    hook_exec_fixture: bool = False
    timeout_seconds: int = 90

    def __post_init__(self) -> None:
        _string_tuple(self.deny_rules, "deny rules")
        _string_tuple(self.setup_commands, "setup commands")
        if type(self.seed_files) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or not item[0]
            or item[0].startswith("/")
            or ".." in item[0].split("/")
            or type(item[1]) is not str
            or len(item[1]) > 8 * 1024 * 1024
            for item in self.seed_files
        ):
            raise ContractDocumentError("permission seed files are invalid")
        if len({item[0] for item in self.seed_files}) != len(self.seed_files):
            raise ContractDocumentError("permission seed paths must be unique")
        for value, label in (
            (self.allow_command, "allow command"),
            (self.probe_command, "probe command"),
        ):
            if type(value) is not str or not value or len(value) > 16_384:
                raise ContractDocumentError(f"permission {label} is invalid")
        if type(self.oracle_profile) is not PermissionOracleProfile:
            raise ContractDocumentError("permission oracle profile is invalid")
        if self.second_probe is not None and (
            type(self.second_probe) is not str or not self.second_probe
        ):
            raise ContractDocumentError("permission second probe is invalid")
        if (
            type(self.probe_tool) is not str
            or not self.probe_tool
            or len(self.probe_tool) > 128
        ):
            raise ContractDocumentError("permission probe tool is invalid")
        if type(self.probe_arguments) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or any(type(part) is not str for part in item)
            for item in self.probe_arguments
        ):
            raise ContractDocumentError("permission probe arguments are invalid")
        if self.expected_token is not None and (
            type(self.expected_token) is not str or not self.expected_token
        ):
            raise ContractDocumentError("permission expected token is invalid")
        if type(self.initialize_git) is not bool:
            raise ContractDocumentError("permission git policy is invalid")
        if type(self.git_baseline_files) is not tuple or any(
            type(item) is not tuple
            or len(item) != 3
            or any(type(part) is not str for part in item)
            or not item[0]
            or item[0].startswith("/")
            or ".." in item[0].split("/")
            for item in self.git_baseline_files
        ):
            raise ContractDocumentError("permission git baseline is invalid")
        if bool(self.git_baseline_files) != self.initialize_git:
            raise ContractDocumentError(
                "permission git baseline and initialization must agree"
            )
        if self.control_dependency is not None and (
            type(self.control_dependency) is not str
            or _ID.fullmatch(self.control_dependency) is None
        ):
            raise ContractDocumentError("permission control dependency is invalid")
        if type(self.dependency_kind) is not DependencyKind:
            raise ContractDocumentError("permission dependency kind is invalid")
        if self.control_dependency is None and self.dependency_kind is not DependencyKind.GATE:
            raise ContractDocumentError("permission dependency kind has no dependency")
        if self.variant_command is not None and (
            type(self.variant_command) is not str or not self.variant_command
        ):
            raise ContractDocumentError("permission variant command is invalid")
        if self.variant_command is not None:
            raise ContractDocumentError(
                "permission isolated variant declaration is incomplete"
            )
        _string_tuple(
            self.counterfactual_setup_commands,
            "counterfactual setup commands",
        )
        _string_tuple(self.settle_paths, "settle paths")
        _string_tuple(self.probe_tokens, "probe tokens")
        for value in (
            self.mcp_marker,
            self.custom_read_external,
            self.hook_exec_fixture,
        ):
            if type(value) is not bool:
                raise ContractDocumentError("permission fixture switch is invalid")
        if bool(self.counterfactual_setup_commands):
            raise ContractDocumentError(
                "permission add-directory counterfactual is incomplete"
            )
        if (
            type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 900
        ):
            raise ContractDocumentError("permission timeout is invalid")


def compile_permission_steps(
    steps: tuple[PermissionStep, ...],
    /,
    *,
    oracle_profile: PermissionOracleProfile,
    control_dependency: str | None = None,
) -> PermissionRecipe:
    """Compile visible permission steps into the runtime recipe."""

    if type(steps) is not tuple or any(type(step) is not PermissionStep for step in steps):
        raise ContractDocumentError("permission steps require exact typed values")
    order = {
        "set_mode": 0,
        "add_deny_rule": 1,
        "seed_file": 2,
        "allow_command": 3,
        "probe_command": 4,
        "probe_tool": 5,
        "capture_target_file": 6,
    }
    operations = tuple(step.operation for step in steps)
    if tuple(order[operation] for operation in operations) != tuple(
        sorted(order[operation] for operation in operations)
    ):
        raise ContractDocumentError("permission probe steps are incomplete or unordered")
    for required in (
        "allow_command",
        "probe_command",
        "probe_tool",
        "capture_target_file",
    ):
        if operations.count(required) != 1:
            raise ContractDocumentError("permission probe steps are incomplete or unordered")

    def text(operation: str) -> str:
        value = next(step.value for step in steps if step.operation == operation)
        if type(value) is not str or not value:
            raise ContractDocumentError(f"permission {operation} step is invalid")
        return value

    setup_commands = tuple(
        step.value for step in steps if step.operation == "set_mode"
    )
    deny_rules = tuple(
        step.value for step in steps if step.operation == "add_deny_rule"
    )
    if any(type(value) is not str or not value for value in setup_commands + deny_rules):
        raise ContractDocumentError("permission setup or deny step is invalid")
    seed_files = tuple(step.value for step in steps if step.operation == "seed_file")
    if any(
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not str
        or not value[0]
        or type(value[1]) is not str
        for value in seed_files
    ):
        raise ContractDocumentError("permission seed-file step is invalid")
    capture = next(
        step.value for step in steps if step.operation == "capture_target_file"
    )
    if (
        type(capture) is not tuple
        or len(capture) != 2
        or type(capture[0]) is not str
        or not capture[0]
        or type(capture[1]) is not bool
    ):
        raise ContractDocumentError("permission target-file capture is invalid")
    # An absence capture must name the marker written by the probe.  A
    # presence/content capture may intentionally observe a file reached via a
    # wildcard, variable expansion, or relative path, so the literal path need
    # not occur in the shell text.
    if not capture[1] and capture[0] not in text("probe_command"):
        raise ContractDocumentError("permission target-file capture differs from its probe")
    return PermissionRecipe(
        deny_rules=deny_rules,
        setup_commands=setup_commands,
        seed_files=seed_files,
        allow_command=text("allow_command"),
        probe_command=text("probe_command"),
        oracle_profile=oracle_profile,
        probe_tool=text("probe_tool"),
        control_dependency=control_dependency,
    )


@dataclass(frozen=True, slots=True)
class PermissionScenario:
    definition: ScenarioDefinition
    recipe: PermissionRecipe

    def __post_init__(self) -> None:
        if (
            type(self.definition) is not ScenarioDefinition
            or type(self.recipe) is not PermissionRecipe
        ):
            raise ContractDocumentError(
                "permission scenario requires exact typed values"
            )
        if self.definition.classification.primary != "permission":
            raise ContractDocumentError(
                "permission scenario owner differs from its Harness layer"
            )


@dataclass(frozen=True, slots=True)
class PermissionRawObservation:
    """Bounded, path-free facts produced by the child execution authority."""

    scenario_id: str
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    request_count: int
    allow_file_present: bool
    deny_file_present: bool
    target_content: str | None
    secondary_target_content: str | None
    escaped_path_present: bool
    target_mode: str | None
    seed_directory_present: bool
    node_present: bool
    memory_directory_present: bool
    directory_present: bool
    probe_tool_advertised: bool
    git_ready: bool
    granted_path_write_present: bool | None
    ungranted_path_write_present: bool | None
    canonical_variant_blocked: bool | None
    rewritten_variant_blocked: bool | None
    rewritten_variant_control_mutated: bool | None
    messages_text: str
    stdout_text: str
    process_tree_clean: bool
    gateway_clean: bool
    workspace_clean: bool
    timed_out: bool
    backend_evidence_digest: str
    mcp_spawned: bool = False
    mcp_wire_events: tuple[str, ...] = ()
    observed_tokens: tuple[str, ...] = ()
    settled_paths: tuple[str, ...] = ()
    hook_events: tuple[str, ...] = ()
    advertised_tools: tuple[str, ...] = ()
    # Harness-specific external facts are carried as a closed, auditable
    # extension point.  They are produced by an observer, never inferred by
    # the oracle from recorded verdicts or free-form model text.
    external_facts: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("permission observation id is invalid")
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
                raise ContractDocumentError("permission observation digest is invalid")
        if type(self.artifact_id) is not str or _ID.fullmatch(self.artifact_id) is None:
            raise ContractDocumentError("permission artifact id is invalid")
        if type(self.request_count) is not int or not 0 <= self.request_count <= 4096:
            raise ContractDocumentError("permission request count is invalid")
        for value in (
            self.allow_file_present,
            self.deny_file_present,
            self.escaped_path_present,
            self.seed_directory_present,
            self.node_present,
            self.memory_directory_present,
            self.directory_present,
            self.probe_tool_advertised,
            self.git_ready,
            self.process_tree_clean,
            self.gateway_clean,
            self.workspace_clean,
            self.timed_out,
        ):
            if type(value) is not bool:
                raise ContractDocumentError("permission boolean fact is invalid")
        for value in (self.target_content, self.secondary_target_content):
            if value is not None and type(value) is not str:
                raise ContractDocumentError("permission target content is invalid")
        if self.target_mode is not None and type(self.target_mode) is not str:
            raise ContractDocumentError("permission target mode is invalid")
        for value in (
            self.canonical_variant_blocked,
            self.rewritten_variant_blocked,
            self.rewritten_variant_control_mutated,
        ):
            if value is not None and type(value) is not bool:
                raise ContractDocumentError("permission variant fact is invalid")
        variant_facts = (
            self.canonical_variant_blocked,
            self.rewritten_variant_blocked,
            self.rewritten_variant_control_mutated,
        )
        if any(value is not None for value in variant_facts) and any(
            value is None for value in variant_facts
        ):
            raise ContractDocumentError("permission variant facts must be complete")
        add_directory_facts = (
            self.granted_path_write_present,
            self.ungranted_path_write_present,
        )
        if any(value is not None for value in add_directory_facts) and any(
            value is None for value in add_directory_facts
        ):
            raise ContractDocumentError(
                "permission add-directory facts must be complete"
            )
        if any(
            value is not None and type(value) is not bool
            for value in add_directory_facts
        ):
            raise ContractDocumentError("permission add-directory fact is invalid")
        if type(self.messages_text) is not str or type(self.stdout_text) is not str:
            raise ContractDocumentError("permission text fact is invalid")
        if type(self.mcp_spawned) is not bool:
            raise ContractDocumentError("permission MCP marker fact is invalid")
        _string_tuple(self.mcp_wire_events, "MCP wire events")
        _string_tuple(self.observed_tokens, "observed tokens")
        _string_tuple(self.settled_paths, "settled paths")
        _string_tuple(self.hook_events, "hook events")
        _string_tuple(self.advertised_tools, "advertised tools", maximum=4096)
        if type(self.external_facts) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or not item[0]
            or type(item[1]) not in {bool, int, str}
            for item in self.external_facts
        ):
            raise ContractDocumentError("permission external facts are invalid")


def define_permission_scenario(
    scenario_id: str, recipe: PermissionRecipe, role: ScenarioRole, /
) -> PermissionScenario:
    if type(scenario_id) is not str or _ID.fullmatch(scenario_id) is None:
        raise ContractDocumentError("permission scenario id is invalid")
    if type(recipe) is not PermissionRecipe or type(role) is not ScenarioRole:
        raise ContractDocumentError("permission scenario declaration is invalid")
    dependencies = (
        ()
        if recipe.control_dependency is None
        else (DependencyRef(recipe.control_dependency, recipe.dependency_kind),)
    )
    definition = ScenarioDefinition(
        scenario_id,
        "1",
        Classification("permission"),
        role,
        1_000_000 if role is ScenarioRole.TARGET else 0,
        ScenarioDifficulty.HARD,
        dependencies,
        ExperimentBinding(
            ComponentRef("permission-child-fixture", "1", ComponentKind.FIXTURE),
            ComponentRef("permission-child-adapter", "1", ComponentKind.STIMULUS),
            ComponentRef("permission-child-observer", "1", ComponentKind.OBSERVER),
            ComponentRef("permission-child-oracle", "1", ComponentKind.ORACLE),
            ComponentRef("permission-child-cleanup", "1", ComponentKind.CLEANUP),
        ),
        UnsupportedPolicy.FAIL,
    )
    return PermissionScenario(definition, recipe)


def evaluation_digest(
    scenario: PermissionScenario, observation: PermissionRawObservation
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
            b"doc2harness.permission-child-evaluation/1\0" + canonical_json(body)
        ).hexdigest()
    )


ORIGINAL_TARGET_CONTENT = _ORIGINAL

__all__ = [
    "ORIGINAL_TARGET_CONTENT",
    "PermissionApplicability",
    "PermissionOracleProfile",
    "PermissionOutcome",
    "PermissionRawObservation",
    "PermissionRecipe",
    "PermissionScenario",
    "PermissionStep",
    "add_deny_rule",
    "allow_command",
    "capture_target_file",
    "compile_permission_steps",
    "define_permission_scenario",
    "evaluation_digest",
    "probe_command",
    "probe_tool",
    "seed_file",
    "set_mode",
]
