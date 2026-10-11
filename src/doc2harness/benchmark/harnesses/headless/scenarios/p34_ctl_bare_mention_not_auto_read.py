"""Verify a bare mention is not automatically read into the request.

1. Run the task containing only a bare file mention.
2. Observe the main process and request records.
3. Require an observable request without the planted file token.
4. Pass only when implicit reading is absent.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "p34_ctl_bare_mention_not_auto_read"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    run = next((r for r in observation.runs if r.label == "main"), None)
    if run is None:
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.INCONCLUSIVE,
            "HEADLESS_MAIN_MISSING",
        )
    if not run.requests:
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.INCONCLUSIVE,
            "HEADLESS_REQUEST_UNOBSERVED",
        )
    present = any(
        RECIPE.probe_token in request.probe_hits or RECIPE.probe_token in request.body
        for request in run.requests
    )
    return HeadlessJudgement(
        HeadlessOutcome.PASS if not present else HeadlessOutcome.FAIL,
        HeadlessApplicability.APPLICABLE,
        "HEADLESS_PROFILE_CHECKED",
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
