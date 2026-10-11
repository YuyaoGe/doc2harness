"""Control for the autocommit-failure trajectory stimulus."""
from doc2harness.benchmark.definition import ScenarioRole
from ..adapter import build_scenario, make_judge

SCENARIO_ID = "trajectory_autocommit_fail_recorded_control"
SCENARIO = build_scenario(SCENARIO_ID, ScenarioRole.CONTROL, None, "seq_probe")
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition
_judge = make_judge(SCENARIO, "autocommit_fail")
def judge(observation):
    return _judge(observation)
__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
