"""A syntactically broken configuration must fail through the normal startup boundary.
1. Declare the clean-start configuration with malformed JSON.
2. Compile the declaration into the startup recipe.
3. Run the startup observer against that recipe.
4. Judge construction, interface, traceback, and cleanup facts without side effects.
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

SCENARIO_ID = "startup_broken_config_file"
STEPS = (
    startup_configuration(StartupProfile.CLEAN_CONSTRUCT, '{"yolo": true,, }'),
)
RECIPE = compile_startup_steps(STEPS)
SCENARIO = define_startup_scenario(SCENARIO_ID, RECIPE, ScenarioRole.TARGET)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: StartupObservation):
    validate_startup_observation(SCENARIO, observation)
    if not observation.result_present or observation.import_traceback:
        return build_startup_evaluation(SCENARIO, observation, False, True, "STARTUP_IMPORT_FAILED")
    if not observation.standard_interface:
        return build_startup_evaluation(SCENARIO, observation, False, False, "STARTUP_INTERFACE_UNOBSERVED")
    return build_startup_evaluation(
        SCENARIO,
        observation,
        observation.constructed and not observation.constructor_traceback,
        True,
        "STARTUP_CONSTRUCT_CHECKED",
    )

__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
