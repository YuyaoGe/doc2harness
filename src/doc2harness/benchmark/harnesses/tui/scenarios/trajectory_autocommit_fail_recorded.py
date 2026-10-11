"""Require an autocommit failure and its cause in the trajectory.

1. Run a write with an isolated failing Git hook or signing program.
2. Observe the persisted trajectory and dynamic failure token.
3. Require explicit auto-commit ownership and failure semantics.
"""
from doc2harness.benchmark.definition import ScenarioRole
from ..adapter import build_scenario, make_judge

SCENARIO_ID = "trajectory_autocommit_fail_recorded"
SCENARIO = build_scenario(SCENARIO_ID, ScenarioRole.TARGET, "trajectory_autocommit_fail_recorded_control", "gpg_fail")
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition
_judge = make_judge(SCENARIO, "autocommit_fail")
def judge(observation):
    return _judge(observation)
__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
