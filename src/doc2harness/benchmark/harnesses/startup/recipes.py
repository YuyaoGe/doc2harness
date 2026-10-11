"""Frozen recipes for the startup Harness."""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole
from .contracts import StartupProfile, StartupRecipe, define_startup_scenario

_GATE = (("launcher_health_control", DependencyKind.GATE),)
_ROWS = {
    "startup_broken_config_file": (
        StartupRecipe(StartupProfile.CLEAN_CONSTRUCT, '{"yolo": true,, }'),
        ScenarioRole.TARGET,
    ),
    "startup_bom_config": (
        StartupRecipe(StartupProfile.CLEAN_CONSTRUCT, '\ufeff{"yolo": true}'),
        ScenarioRole.TARGET,
    ),
    "launcher_health_control": (
        StartupRecipe(StartupProfile.LAUNCHER_HEALTH, launcher_probe="control", timeout_seconds=25),
        ScenarioRole.CONTROL,
    ),
    "launcher_python_minimum_fallback": (
        StartupRecipe(
            StartupProfile.PYTHON_MINIMUM,
            launcher_probe="python-floor",
            dependencies=_GATE,
            timeout_seconds=25,
        ),
        ScenarioRole.TARGET,
    ),
    "launcher_ambient_python_isolated": (
        StartupRecipe(
            StartupProfile.AMBIENT_PYTHON,
            launcher_probe="ambient-python",
            dependencies=_GATE,
            timeout_seconds=25,
        ),
        ScenarioRole.TARGET,
    ),
}
SCENARIOS = tuple(
    (define_startup_scenario(test_id, recipe, role) for test_id, (recipe, role) in _ROWS.items())
)
_BY_ID = {scenario.definition.scenario_id: scenario for scenario in SCENARIOS}


def scenario_by_id(scenario_id: str) -> object:
    try:
        return _BY_ID[scenario_id]
    except KeyError as error:
        raise KeyError(f"unknown startup scenario: {scenario_id}") from error
