"""The launcher must fall back safely when probing below the supported Python floor.
1. Declare the launcher Python-floor probe and its health-control dependency.
2. Compile the declaration into the startup recipe.
3. Run the startup observer against that recipe.
4. Judge fallback selection, version facts, traceback, and cleanup without side effects.
"""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole
from doc2harness.benchmark.harnesses.startup.contracts import (
    StartupObservation,
    StartupProfile,
    build_startup_evaluation,
    compile_startup_steps,
    define_startup_scenario,
    startup_configuration,
    validate_startup_observation,
)

SCENARIO_ID = "launcher_python_minimum_fallback"
STEPS = (
    startup_configuration(
        StartupProfile.PYTHON_MINIMUM,
        launcher_probe="python-floor",
        dependencies=(("launcher_health_control", DependencyKind.GATE),),
        timeout_seconds=25,
    ),
)
RECIPE = compile_startup_steps(STEPS)
SCENARIO = define_startup_scenario(SCENARIO_ID, RECIPE, ScenarioRole.TARGET)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: StartupObservation):
    validate_startup_observation(SCENARIO, observation)
    if not observation.launcher_available:
        return build_startup_evaluation(SCENARIO, observation, False, False, "LAUNCHER_UNOBSERVED")
    if not observation.fixture_verified:
        return build_startup_evaluation(SCENARIO, observation, False, True, "LAUNCHER_FIXTURE_FAILED")
    if not observation.declared_floor:
        return build_startup_evaluation(SCENARIO, observation, False, False, "PYTHON_FLOOR_UNOBSERVED")
    passed = (
        observation.exact_entry
        and observation.path_selected
        and not observation.wrong_entry
        and observation.old_python_probed
        and not observation.old_python_entered
        and observation.healthy_version_satisfies
    )
    return build_startup_evaluation(SCENARIO, observation, passed, True, "PYTHON_FLOOR_CHECKED")

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
