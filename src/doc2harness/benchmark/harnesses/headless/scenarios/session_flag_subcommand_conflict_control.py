"""Check that at least one management subcommand has a healthy baseline.

1. Run memory status and config list without a session flag.
2. Observe both external return codes in one isolated home/workspace.
3. Pass when either management face returns rc0; otherwise remain inconclusive.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability, HeadlessJudgement, HeadlessObservation,
    HeadlessOutcome, HeadlessScenario,
)
from doc2harness.benchmark.harnesses.headless.recipes import scenario_by_id

SCENARIO_ID = "session_flag_subcommand_conflict_control"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
scenario = SCENARIO
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    if any(run.rc == 0 for run in observation.runs[:2]):
        return HeadlessJudgement(HeadlessOutcome.PASS, HeadlessApplicability.APPLICABLE, "SESSION_FLAG_BASELINE_HEALTHY")
    return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.INCONCLUSIVE, "SESSION_FLAG_BASELINE_UNAVAILABLE")


__all__ = ["RECIPE", "STEPS", "definition", "scenario", "SCENARIO", "SCENARIO_ID", "judge"]
