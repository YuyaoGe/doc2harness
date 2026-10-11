"""The launcher health control verifies availability and selects the intended entry.
1. Declare the launcher health probe and its short timeout.
2. Compile the declaration into the startup recipe.
3. Run the startup observer against that recipe.
4. Judge launcher, fixture, entry, path, and cleanup facts without side effects.
"""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.startup.contracts import (
    StartupObservation,
    StartupProfile,
    build_startup_evaluation,
    compile_startup_steps,
    define_startup_scenario,
    startup_configuration,
    validate_startup_observation,
)

SCENARIO_ID = "launcher_health_control"
STEPS = (
    startup_configuration(
        StartupProfile.LAUNCHER_HEALTH,
        launcher_probe="control",
        timeout_seconds=25,
    ),
)
RECIPE = compile_startup_steps(STEPS)
SCENARIO = define_startup_scenario(SCENARIO_ID, RECIPE, ScenarioRole.CONTROL)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: StartupObservation):
    validate_startup_observation(SCENARIO, observation)
    if not observation.launcher_available:
        return build_startup_evaluation(SCENARIO, observation, False, False, "LAUNCHER_UNOBSERVED")
    if not observation.fixture_verified:
        return build_startup_evaluation(SCENARIO, observation, False, True, "LAUNCHER_FIXTURE_FAILED")
    if observation.exact_entry and not observation.path_selected:
        return build_startup_evaluation(SCENARIO, observation, False, False, "LAUNCHER_PATH_UNOBSERVED")
    return build_startup_evaluation(
        SCENARIO,
        observation,
        observation.exact_entry and observation.path_selected and not observation.wrong_entry,
        True,
        "LAUNCHER_HEALTH_CHECKED",
    )

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
