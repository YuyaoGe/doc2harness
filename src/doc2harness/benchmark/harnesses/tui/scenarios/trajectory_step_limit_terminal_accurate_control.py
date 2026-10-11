"""Control for an externally observed step-limit terminal trajectory.

1. Run the bounded PTY conversation against the normal mock gateway.
2. Observe requests, exit status, cleanup, and trajectory files.
3. Establish that the stimulus and terminal channel are live.
"""
from doc2harness.benchmark.definition import ScenarioRole
from ..adapter import build_scenario, make_judge

SCENARIO_ID = "trajectory_step_limit_terminal_accurate_control"
SCENARIO = build_scenario(SCENARIO_ID, ScenarioRole.CONTROL, None, "seq_probe")
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition
_judge = make_judge(SCENARIO, "step_limit")
def judge(observation):
    return _judge(observation)
__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
