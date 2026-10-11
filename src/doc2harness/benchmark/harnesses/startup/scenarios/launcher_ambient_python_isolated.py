"""The launcher must isolate the selected Python from ambient path and home state.
1. Declare the ambient-Python isolation probe and its health-control dependency.
2. Compile the declaration into the startup recipe.
3. Run the startup observer against that recipe.
4. Judge entry selection, poison checks, isolation, and cleanup without side effects.
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

SCENARIO_ID = "launcher_ambient_python_isolated"
STEPS = (
    startup_configuration(
        StartupProfile.AMBIENT_PYTHON,
        launcher_probe="ambient-python",
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
    passed = (
        observation.exact_entry
        and observation.poison_selftest
        and observation.ambient_path_isolated
        and observation.ambient_home_isolated
    )
    return build_startup_evaluation(SCENARIO, observation, passed, True, "AMBIENT_PYTHON_CHECKED")

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
