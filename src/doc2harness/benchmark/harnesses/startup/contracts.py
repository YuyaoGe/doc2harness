"""Typed declarations and observations for startup behavior."""

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
from doc2harness.runtime.errors import RuntimeBoundaryError

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z", re.ASCII)


class StartupProfile(str, Enum):
    CLEAN_CONSTRUCT = "CLEAN_CONSTRUCT"
    LAUNCHER_HEALTH = "LAUNCHER_HEALTH"
    PYTHON_MINIMUM = "PYTHON_MINIMUM"
    AMBIENT_PYTHON = "AMBIENT_PYTHON"


class StartupOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


class StartupApplicability(str, Enum):
    APPLICABLE = "APPLICABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class StartupRecipe:
    profile: StartupProfile
    config_text: str | None = None
    home_files: tuple[tuple[str, str], ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    tripwire: bool = False
    launcher_probe: str | None = None
    dependencies: tuple[tuple[str, DependencyKind], ...] = ()
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if type(self.profile) is not StartupProfile:
            raise ContractDocumentError("startup profile is invalid")
        if self.config_text is not None and type(self.config_text) is not str:
            raise ContractDocumentError("startup config text is invalid")
        for name, value in (("home files", self.home_files), ("environment", self.environment)):
            if type(value) is not tuple or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or not item[0]
                or type(item[1]) is not str
                for item in value
            ):
                raise ContractDocumentError(f"startup {name} are invalid")
        if type(self.tripwire) is not bool:
            raise ContractDocumentError("startup tripwire flag is invalid")
        if self.launcher_probe is not None and (
            type(self.launcher_probe) is not str or not self.launcher_probe
        ):
            raise ContractDocumentError("startup launcher probe is invalid")
        if type(self.dependencies) is not tuple or any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not DependencyKind
            for item in self.dependencies
        ):
            raise ContractDocumentError("startup dependencies are invalid")
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise ContractDocumentError("startup timeout is invalid")


@dataclass(frozen=True, slots=True)
class StartupConfigurationStep:
    """Closed authoring step for one startup configuration/probe."""

    recipe: StartupRecipe

    def __post_init__(self) -> None:
        if type(self.recipe) is not StartupRecipe:
            raise ContractDocumentError("startup configuration step is invalid")


def startup_configuration(
    profile: StartupProfile,
    config_text: str | None = None,
    *,
    home_files: tuple[tuple[str, str], ...] = (),
    environment: tuple[tuple[str, str], ...] = (),
    tripwire: bool = False,
    launcher_probe: str | None = None,
    dependencies: tuple[tuple[str, DependencyKind], ...] = (),
    timeout_seconds: int = 120,
) -> StartupConfigurationStep:
    """Declare the startup setup/probe while keeping recipe compilation centralized."""

    return StartupConfigurationStep(
        StartupRecipe(
            profile,
            config_text,
            home_files,
            environment,
            tripwire,
            launcher_probe,
            dependencies,
            timeout_seconds,
        )
    )


def compile_startup_steps(
    steps: tuple[StartupConfigurationStep, ...], /
) -> StartupRecipe:
    """Compile the closed startup declaration into the recipe object."""

    if type(steps) is not tuple or len(steps) != 1 or type(steps[0]) is not StartupConfigurationStep:
        raise ContractDocumentError("startup steps must contain one configuration operation")
    return steps[0].recipe


@dataclass(frozen=True, slots=True)
class StartupScenario:
    definition: ScenarioDefinition
    recipe: StartupRecipe

    def __post_init__(self) -> None:
        if type(self.definition) is not ScenarioDefinition or type(self.recipe) is not StartupRecipe:
            raise ContractDocumentError("startup scenario requires exact typed values")
        if self.definition.classification.primary != "startup":
            raise ContractDocumentError("startup scenario owner differs from its Harness")


@dataclass(frozen=True, slots=True)
class StartupObservation:
    scenario_id: str
    plan_digest: str
    artifact_id: str
    artifact_digest: str
    backend_evidence_digest: str
    result_present: bool
    import_traceback: bool
    standard_interface: bool
    constructed: bool
    constructor_traceback: bool
    request_count: int | None
    launcher_available: bool
    fixture_verified: bool
    exact_entry: bool
    path_selected: bool
    wrong_entry: bool
    declared_floor: bool
    old_python_probed: bool
    old_python_entered: bool
    healthy_version_satisfies: bool
    poison_selftest: bool
    ambient_path_isolated: bool
    ambient_home_isolated: bool
    process_tree_clean: bool
    workspace_clean: bool
    # Installation evidence is optional for profiles that do not install.  Keeping
    # these typed facts separate prevents a successful installer from being
    # inferred from a launcher probe or synthetic output.
    install_attempted: bool = False
    install_rc: int | None = None
    post_install_rc: int | None = None
    installed_command_observed: bool = False
    installed_brand_observed: bool = False
    mini_swe_agent_leak: bool = False
    install_artifact_unchanged: bool = True
    install_cross_artifact_isolated: bool = True
    install_artifact_observed: bool = False
    install_cross_artifact_checked: bool = False

    def __post_init__(self) -> None:
        if type(self.scenario_id) is not str or _ID.fullmatch(self.scenario_id) is None:
            raise ContractDocumentError("startup observation id is invalid")
        for name, value in (
            ("plan", self.plan_digest),
            ("artifact", self.artifact_digest),
            ("backend evidence", self.backend_evidence_digest),
        ):
            if type(value) is not str or not value.startswith("sha256:") or len(value) != 71:
                raise ContractDocumentError(f"startup {name} digest is invalid")
        if type(self.artifact_id) is not str or not self.artifact_id:
            raise ContractDocumentError("startup artifact id is invalid")
        boolean_fields = (
            self.result_present,
            self.import_traceback,
            self.standard_interface,
            self.constructed,
            self.constructor_traceback,
            self.launcher_available,
            self.fixture_verified,
            self.exact_entry,
            self.path_selected,
            self.wrong_entry,
            self.declared_floor,
            self.old_python_probed,
            self.old_python_entered,
            self.healthy_version_satisfies,
            self.poison_selftest,
            self.ambient_path_isolated,
            self.ambient_home_isolated,
            self.process_tree_clean,
            self.workspace_clean,
        )
        if any(type(value) is not bool for value in boolean_fields):
            raise ContractDocumentError("startup observation booleans are invalid")
        if self.request_count is not None and (
            type(self.request_count) is not int or self.request_count < 0
        ):
            raise ContractDocumentError("startup request count is invalid")
        for name, value in (("install rc", self.install_rc), ("post-install rc", self.post_install_rc)):
            if value is not None and (type(value) is not int):
                raise ContractDocumentError(f"startup {name} is invalid")
        install_booleans = (
            self.install_attempted,
            self.installed_command_observed,
            self.installed_brand_observed,
            self.mini_swe_agent_leak,
            self.install_artifact_unchanged,
            self.install_cross_artifact_isolated,
            self.install_artifact_observed,
            self.install_cross_artifact_checked,
        )
        if any(type(value) is not bool for value in install_booleans):
            raise ContractDocumentError("startup installation facts are invalid")


@dataclass(frozen=True, slots=True)
class StartupEvaluation:
    scenario_id: str
    outcome: StartupOutcome
    applicability: StartupApplicability
    reason_code: str
    evidence_digest: str
    contribution_microunits: int


def define_startup_scenario(
    scenario_id: str, recipe: StartupRecipe, role: ScenarioRole, /
) -> StartupScenario:
    dependencies = tuple(
        sorted(
            (DependencyRef(test_id, kind) for test_id, kind in recipe.dependencies),
            key=lambda item: (item.scenario_id, item.kind.value),
        )
    )
    definition = ScenarioDefinition(
        scenario_id,
        "1",
        Classification("startup"),
        role,
        1_000_000 if role is ScenarioRole.TARGET else 0,
        ScenarioDifficulty.HARD,
        dependencies,
        ExperimentBinding(
            ComponentRef("startup-fixture", "1", ComponentKind.FIXTURE),
            ComponentRef("startup-adapter", "1", ComponentKind.STIMULUS),
            ComponentRef("startup-observer", "1", ComponentKind.OBSERVER),
            ComponentRef("startup-oracle", "1", ComponentKind.ORACLE),
            ComponentRef("startup-cleanup", "1", ComponentKind.CLEANUP),
        ),
        UnsupportedPolicy.FAIL,
    )
    return StartupScenario(definition, recipe)


def evaluation_digest(scenario: StartupScenario, observation: StartupObservation) -> str:
    body = {
        "artifact_digest": observation.artifact_digest,
        "backend_evidence_digest": observation.backend_evidence_digest,
        "definition_digest": definition_digest(scenario.definition),
        "plan_digest": observation.plan_digest,
        "scenario_id": scenario.definition.scenario_id,
    }
    return "sha256:" + hashlib.sha256(
        b"doc2harness.startup-evaluation/1\0" + canonical_json(body)
    ).hexdigest()


def validate_startup_observation(
    scenario: StartupScenario,
    observation: StartupObservation,
    /,
) -> None:
    """Validate shared scenario/evidence identity before a local judge runs."""

    if type(scenario) is not StartupScenario:
        raise ContractDocumentError("startup judge requires an exact scenario")
    if type(observation) is not StartupObservation:
        raise RuntimeBoundaryError(
            "SBH-STARTUP-EVIDENCE-TYPE",
            "startup observer returned invalid evidence",
        )
    if observation.scenario_id != scenario.definition.scenario_id:
        raise RuntimeBoundaryError(
            "SBH-STARTUP-EVIDENCE-MISMATCH",
            "startup evidence belongs to another scenario",
        )


def build_startup_evaluation(
    scenario: StartupScenario,
    observation: StartupObservation,
    passed: bool,
    applicable: bool,
    reason: str,
    /,
) -> StartupEvaluation:
    """Apply shared cleanup and contribution policy after local profile judgment."""

    validate_startup_observation(scenario, observation)
    if type(passed) is not bool or type(applicable) is not bool:
        raise ContractDocumentError("startup judge decisions must be exact booleans")
    if type(reason) is not str or not reason:
        raise ContractDocumentError("startup judge reason must be non-empty")
    if applicable and (
        not observation.process_tree_clean or not observation.workspace_clean
    ):
        passed = False
        reason = "STARTUP_CLEANUP_FAILED"
    applicability = (
        StartupApplicability.APPLICABLE
        if applicable
        else StartupApplicability.INCONCLUSIVE
    )
    contribution = scenario.definition.weight_microunits if applicable else 0
    if scenario.definition.role is ScenarioRole.CONTROL:
        contribution = 0
    return StartupEvaluation(
        scenario.definition.scenario_id,
        StartupOutcome.PASS if passed else StartupOutcome.FAIL,
        applicability,
        reason,
        evaluation_digest(scenario, observation),
        contribution,
    )
