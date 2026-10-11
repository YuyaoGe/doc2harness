"""Frozen recipes for extension definition symlink counterfactuals."""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole

from .contracts import ExtensionCounterfactualRecipe, define_extension_scenario

_SETUP = ("/mode yolo", "/extsymcommand", "/skill extsymskill", "/extsymplugin")
_ALLOW = "echo PERM_LIVE_7755 > live.txt"

_ROWS = {
    "extension_inside_symlink_discovery_control": (
        ExtensionCounterfactualRecipe(_SETUP, "bash", _ALLOW, False),
        ScenarioRole.CONTROL,
    ),
    "extension_definition_symlink_outside_fenced": (
        ExtensionCounterfactualRecipe(
            _SETUP,
            "bash",
            _ALLOW,
            True,
            (("extension_inside_symlink_discovery_control", DependencyKind.GATE),),
        ),
        ScenarioRole.TARGET,
    ),
}

SCENARIOS = tuple(
    define_extension_scenario(test_id, recipe, role)
    for test_id, (recipe, role) in _ROWS.items()
)
_BY_ID = {scenario.definition.scenario_id: scenario for scenario in SCENARIOS}


def scenario_by_id(scenario_id: str):
    try:
        return _BY_ID[scenario_id]
    except KeyError as error:
        raise KeyError(f"unknown extension counterfactual scenario: {scenario_id}") from error


__all__ = ["SCENARIOS", "scenario_by_id"]
