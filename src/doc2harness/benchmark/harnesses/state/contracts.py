"""Closed declarations and observations for child-process state scenarios."""

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


class StateOracleProfile(str, Enum):
    REQUEST_TOKEN_PRESENT = "REQUEST_TOKEN_PRESENT"
    CUSTOM_COMMAND_FACE = "CUSTOM_COMMAND_FACE"
    STRUCTURED_FACTS = "STRUCTURED_FACTS"
    CUSTOM_SINGLE_EXPANSION = "CUSTOM_SINGLE_EXPANSION"
    CUSTOM_NO_SECONDARY = "CUSTOM_NO_SECONDARY"


class StateOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class StateApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


def _validate_frozen(value: FrozenValue, depth: int = 0) -> None:
    if depth > 18:
        raise ContractDocumentError("state frozen value nesting is excessive")
    if value is None or type(value) in {str, int, bool}:
        if type(value) is str and len(value) > 8 * 1024 * 1024:
            raise ContractDocumentError("state frozen text is excessive")
        return
    if type(value) is not tuple or len(value) > 8192:
        raise ContractDocumentError("state frozen value is invalid")
    for item in value:
        if type(item) is tuple and len(item) == 2 and type(item[0]) is str:
            _validate_frozen(item[1], depth + 1)
        else:
            _validate_frozen(item, depth + 1)


def _pairs(value: object, label: str) -> tuple[tuple[str, FrozenValue], ...]:
    if type(value) is not tuple or any(
        type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
        for item in value
    ):
        raise ContractDocumentError(f"state {label} is invalid")
    if len({item[0] for item in value}) != len(value):
        raise ContractDocumentError(f"state {label} keys are duplicated")
    for _, item in value:
        _validate_frozen(item)
    return value


@dataclass(frozen=True, slots=True)
class StateStep:
    """One closed, serializable action or observation in a state scenario."""

    operation: str
    value: FrozenValue

    def __post_init__(self) -> None:
        if type(self.operation) is not str or not self.operation:
            raise ContractDocumentError("state step operation is invalid")
        if self.operation not in {
            "seed_file",
            "command",
            "premsgs",
            "api_call",
            "observe_config",
            "observe_file",
            "observe_requests",
            "structured_fact",
            "home_seed_file",
        }:
            raise ContractDocumentError("state step operation is unsupported")
        _validate_frozen(self.value)


def seed_file(path: str, content: str) -> StateStep:
    """Declare a path-relative file that the state fixture must contain."""

    return StateStep("seed_file", (path, content))


def home_seed_file(path: str, content: str) -> StateStep:
    """Declare a file the fixture must place under the isolated HOME.

    Distinct from ``seed_file``, which writes into the workspace.  Extension
    resolution is defined over exactly that boundary - a user-level definition
    lives under HOME, a project-level one under the workspace - so a scenario
    that folds the two together can no longer state which side it is testing.
    """

    return StateStep("home_seed_file", (path, content))


def command(value: str) -> StateStep:
    """Declare one slash command sent to the state artifact."""

    return StateStep("command", value)


def premsgs(value: FrozenValue) -> StateStep:
    """Declare pre-seeded conversation messages for the state session."""

    return StateStep("premsgs", value)


def api_call(value: FrozenValue) -> StateStep:
    """Declare a typed Agent API call description."""

    return StateStep("api_call", value)


def observe_config(key: str) -> StateStep:
    """Declare a configuration key whose post-run value is observed."""

    return StateStep("observe_config", key)


def observe_file(path: str) -> StateStep:
    """Declare a workspace file whose post-run content is observed."""

    return StateStep("observe_file", path)


def observe_requests() -> StateStep:
    """Declare recorder-owned request facts as an observation boundary."""

    return StateStep("observe_requests", "requests")


def structured_fact(name: str) -> StateStep:
    """Declare one structured fact consumed by the scenario-local judge."""

    return StateStep("structured_fact", name)


@dataclass(frozen=True, slots=True)
class StateRecipe:
    commands: tuple[str, ...]
    seed_files: tuple[tuple[str, str], ...]
    config_keys: tuple[str, ...]
    observed_files: tuple[str, ...]
    oracle_profile: StateOracleProfile
    oracle_parameters: tuple[tuple[str, FrozenValue], ...]
    execution_parameters: tuple[tuple[str, FrozenValue], ...] = ()
    dependencies: tuple[tuple[str, DependencyKind], ...] = ()
    timeout_seconds: int = 60
    home_seed_files: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.commands) is not tuple or any(type(x) is not str or not x for x in self.commands):
            raise ContractDocumentError("state commands are invalid")
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
            raise ContractDocumentError("state seed files are invalid")
        if type(self.home_seed_files) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or not item[0]
            or item[0].startswith("/")
            or ".." in item[0].split("/")
            or type(item[1]) is not str
            for item in self.home_seed_files
        ):
            raise ContractDocumentError("state home seed files are invalid")
        for value, label in ((self.config_keys, "config keys"), (self.observed_files, "observed files")):
            if type(value) is not tuple or any(type(x) is not str or not x for x in value):
                raise ContractDocumentError(f"state {label} are invalid")
        if type(self.oracle_profile) is not StateOracleProfile:
            raise ContractDocumentError("state oracle profile is invalid")
        _pairs(self.oracle_parameters, "oracle parameters")
        _pairs(self.execution_parameters, "execution parameters")
        if type(self.dependencies) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _ID.fullmatch(item[0]) is None
            or type(item[1]) is not DependencyKind
            for item in self.dependencies
        ):
            raise ContractDocumentError("state dependencies are invalid")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 900:
            raise ContractDocumentError("state timeout is invalid")


def compile_state_steps(
    steps: tuple[StateStep, ...],
    *,
    oracle_profile: StateOracleProfile,
    oracle_parameters: tuple[tuple[str, FrozenValue], ...],
    execution_parameters: tuple[tuple[str, FrozenValue], ...] = (),
    dependencies: tuple[tuple[str, DependencyKind], ...] = (),
    timeout_seconds: int = 60,
) -> StateRecipe:
    """Compile declarative state steps into the recipe contract.

    Commands, fixture files, config observations, and file observations are
    derived from the visible steps.  The structured-facts execution matrix is
    intentionally supplied as a typed argument because it belongs to the
    shared state producer rather than to the scenario's action sequence.
    """

    if type(steps) is not tuple or any(type(step) is not StateStep for step in steps):
        raise ContractDocumentError("state steps are invalid")
    commands = tuple(step.value for step in steps if step.operation == "command")
    seed_files = tuple(
        step.value for step in steps if step.operation == "seed_file"
    )
    home_seed_files = tuple(
        step.value for step in steps if step.operation == "home_seed_file"
    )
    config_keys = tuple(
        step.value for step in steps if step.operation == "observe_config"
    )
    observed_files = tuple(
        step.value for step in steps if step.operation == "observe_file"
    )
    if any(type(value) is not str or not value for value in commands + config_keys + observed_files):
        raise ContractDocumentError("state step text is invalid")
    if any(
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not str
        or type(value[1]) is not str
        for value in seed_files + home_seed_files
    ):
        raise ContractDocumentError("state seed-file step is invalid")
    return StateRecipe(
        commands=commands,
        seed_files=seed_files,
        home_seed_files=home_seed_files,
        config_keys=config_keys,
        observed_files=observed_files,
        oracle_profile=oracle_profile,
        oracle_parameters=oracle_parameters,
        execution_parameters=execution_parameters,
        dependencies=dependencies,
        timeout_seconds=timeout_seconds,
    )


@dataclass(frozen=True, slots=True)
class StateScenario:
    definition: ScenarioDefinition
    recipe: StateRecipe

    def __post_init__(self) -> None:
        if type(self.definition) is not ScenarioDefinition or type(self.recipe) is not StateRecipe:
            raise ContractDocumentError("state scenario requires exact typed values")
        if self.definition.classification.primary != "state":
            raise ContractDocumentError("state scenario owner differs from its Harness")


@dataclass(frozen=True, slots=True)
class StateObservation:
    scenario_id: str
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    backend_evidence_digest: str
    fixture_verified: bool
    facts: tuple[tuple[str, FrozenValue], ...]
    process_tree_clean: bool
    workspace_clean: bool

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("state observation id is invalid")
        for label, value in (("plan", self.plan_digest), ("artifact", self.artifact_digest), ("backend", self.backend_evidence_digest)):
            if type(value) is not str or not value.startswith("sha256:") or len(value) != 71:
                raise ContractDocumentError(f"state {label} digest is invalid")
        if type(self.artifact_id) is not str or not self.artifact_id:
            raise ContractDocumentError("state artifact id is invalid")
        if type(self.fixture_verified) is not bool:
            raise ContractDocumentError("state fixture fact is invalid")
        _pairs(self.facts, "observation facts")
        if type(self.process_tree_clean) is not bool or type(self.workspace_clean) is not bool:
            raise ContractDocumentError("state cleanup facts are invalid")


@dataclass(frozen=True, slots=True)
class StateEvaluation:
    scenario_id: str
    outcome: StateOutcome
    applicability: StateApplicability
    reason_code: str
    evidence_digest: str
    contribution_microunits: int

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("state evaluation id is invalid")
        if type(self.outcome) is not StateOutcome or type(self.applicability) is not StateApplicability:
            raise ContractDocumentError("state evaluation closed value is invalid")
        if type(self.reason_code) is not str or not self.reason_code or len(self.reason_code) > 128:
            raise ContractDocumentError("state reason is invalid")
        if type(self.evidence_digest) is not str or not self.evidence_digest.startswith("sha256:") or len(self.evidence_digest) != 71:
            raise ContractDocumentError("state evaluation digest is invalid")
        if self.contribution_microunits not in {0, 1_000_000}:
            raise ContractDocumentError("state contribution is invalid")


def define_state_scenario(scenario_id: str, recipe: StateRecipe, role: ScenarioRole, /) -> StateScenario:
    if type(scenario_id) is not str or _ID.fullmatch(scenario_id) is None:
        raise ContractDocumentError("state scenario id is invalid")
    dependencies = tuple(sorted((DependencyRef(test_id, kind) for test_id, kind in recipe.dependencies), key=lambda x: (x.scenario_id, x.kind.value)))
    definition = ScenarioDefinition(
        scenario_id,
        "1",
        Classification("state"),
        role,
        1_000_000 if role is ScenarioRole.TARGET else 0,
        ScenarioDifficulty.HARD,
        dependencies,
        ExperimentBinding(
            ComponentRef("state-child-fixture", "1", ComponentKind.FIXTURE),
            ComponentRef("state-child-adapter", "1", ComponentKind.STIMULUS),
            ComponentRef("state-child-observer", "1", ComponentKind.OBSERVER),
            ComponentRef("state-child-oracle", "1", ComponentKind.ORACLE),
            ComponentRef("state-child-cleanup", "1", ComponentKind.CLEANUP),
        ),
        UnsupportedPolicy.FAIL,
    )
    return StateScenario(definition, recipe)


def evaluation_digest(scenario: StateScenario, observation: StateObservation) -> str:
    body = {
        "artifact_digest": observation.artifact_digest,
        "backend_evidence_digest": observation.backend_evidence_digest,
        "definition_digest": definition_digest(scenario.definition),
        "plan_digest": observation.plan_digest,
        "scenario_id": scenario.definition.scenario_id,
    }
    return "sha256:" + hashlib.sha256(b"doc2harness.state-child-evaluation/1\0" + canonical_json(body)).hexdigest()


__all__ = [
    "FrozenValue", "StateApplicability", "StateEvaluation", "StateObservation",
    "StateOracleProfile", "StateOutcome", "StateRecipe",
    "StateScenario", "StateStep", "api_call", "command", "compile_state_steps",
    "define_state_scenario", "evaluation_digest", "observe_config", "observe_file",
    "observe_requests", "premsgs", "seed_file", "structured_fact",
]
