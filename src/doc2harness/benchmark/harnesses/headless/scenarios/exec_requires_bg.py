"""Check that an orphan --exec is rejected without executing its command.

1. Run the healthy background control and then the orphan foreground form.
2. Observe each external return code and marker path in the shared workspace.
3. Require a closed control and accept only target rc2 with no target marker.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability, HeadlessJudgement, HeadlessObservation,
    HeadlessOutcome, HeadlessScenario,
)
from doc2harness.benchmark.harnesses.headless.recipes import scenario_by_id

SCENARIO_ID = "exec_requires_bg"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
scenario = SCENARIO
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    if len(observation.runs) < 2:
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.INCONCLUSIVE, "EXEC_BG_TARGET_UNOBSERVED")
    control, target = observation.runs[:2]
    if not (control.rc == 0 and "control.marker" in control.written_paths):
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.INCONCLUSIVE, "EXEC_BG_CONTROL_GATE_UNSATISFIED")
    if "target.marker" in target.written_paths:
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "EXEC_BG_ORPHAN_EXECUTED")
    if target.rc == 2:
        return HeadlessJudgement(HeadlessOutcome.PASS, HeadlessApplicability.APPLICABLE, "EXEC_BG_ORPHAN_REJECTED")
    return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "EXEC_BG_ORPHAN_NOT_REJECTED")


__all__ = ["RECIPE", "STEPS", "definition", "scenario", "SCENARIO", "SCENARIO_ID", "judge"]
