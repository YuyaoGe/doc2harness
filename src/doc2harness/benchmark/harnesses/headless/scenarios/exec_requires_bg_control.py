"""Check that the explicit background form executes and writes its marker.

1. Launch the artifact with ``--bg ... --exec touch control.marker``.
2. Observe the external return code and workspace-relative written paths.
3. Pass only when the process succeeds and the marker is present.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability, HeadlessJudgement, HeadlessObservation,
    HeadlessOutcome, HeadlessScenario,
)
from doc2harness.benchmark.harnesses.headless.recipes import scenario_by_id

SCENARIO_ID = "exec_requires_bg_control"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
scenario = SCENARIO
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    run = observation.runs[0] if observation.runs else None
    if run is None:
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.INCONCLUSIVE, "EXEC_BG_CONTROL_UNOBSERVED")
    marker = "control.marker" in run.written_paths
    if run.rc == 0 and marker:
        return HeadlessJudgement(HeadlessOutcome.PASS, HeadlessApplicability.APPLICABLE, "EXEC_BG_CONTROL_CLOSED")
    if marker and run.rc != 0:
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "EXEC_BG_CONTROL_CONTRADICTION")
    return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.INCONCLUSIVE, "EXEC_BG_CONTROL_UNOBSERVED")


__all__ = ["RECIPE", "STEPS", "definition", "scenario", "SCENARIO", "SCENARIO_ID", "judge"]
