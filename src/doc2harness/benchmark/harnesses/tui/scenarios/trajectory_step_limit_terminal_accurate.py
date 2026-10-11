"""Require an explicit step-limit terminal event in the trajectory.

1. Run the bounded PTY conversation against the normal mock gateway.
2. Observe the externally persisted trajectory rather than recorder facts.
3. Require both a limit marker and a terminal event; ordinary completion is insufficient.
"""
from doc2harness.benchmark.definition import ScenarioRole
from ..adapter import build_scenario, make_judge

SCENARIO_ID = "trajectory_step_limit_terminal_accurate"
SCENARIO = build_scenario(SCENARIO_ID, ScenarioRole.TARGET, "trajectory_step_limit_terminal_accurate_control", "seq_probe")
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition
_judge = make_judge(SCENARIO, "step_limit")
def judge(observation):
    return _judge(observation)
__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
