"""Check that --resume is not silently discarded by management commands.

1. Run each management command once without and once with a nonexistent session.
2. Observe return codes and output text for every baseline/flagged pair.
3. Reject silent rc0 output identical to baseline; accept rc2 or an explicit session error.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability, HeadlessJudgement, HeadlessObservation,
    HeadlessOutcome, HeadlessScenario,
)
from doc2harness.benchmark.harnesses.headless.recipes import scenario_by_id

SCENARIO_ID = "session_flag_subcommand_conflict"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
scenario = SCENARIO
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
definition = SCENARIO.definition


def _face_verdict(base, flagged):
    if flagged.rc == 2:
        return "PASS"
    blob = (flagged.stdout + "\n" + flagged.stderr).lower()
    mentions = any(token in blob for token in ("resume", "session", "conflict", "互斥", "会话"))
    if flagged.rc is not None and flagged.rc != 0 and mentions:
        return "PASS"
    if flagged.rc == 0 and flagged.stdout.strip() == base.stdout.strip() and not mentions:
        return "FAIL"
    return "INCONCLUSIVE"


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    if len(observation.runs) < 4:
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.INCONCLUSIVE, "SESSION_FLAG_MATRIX_INCOMPLETE")
    verdicts = tuple(_face_verdict(observation.runs[index], observation.runs[index + 1]) for index in (0, 2))
    if "FAIL" in verdicts:
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "SESSION_FLAGS_SILENTLY_DROPPED")
    if "PASS" in verdicts:
        return HeadlessJudgement(HeadlessOutcome.PASS, HeadlessApplicability.APPLICABLE, "SESSION_FLAGS_REJECTED")
    return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.INCONCLUSIVE, "SESSION_FLAGS_NOT_OBSERVED")


__all__ = ["RECIPE", "STEPS", "definition", "scenario", "SCENARIO", "SCENARIO_ID", "judge"]
