"""Verify an @file reference reaches the headless model request.

1. Seed the referenced file and send the summarization task.
2. Observe the main process and typed request records.
3. Require a successful run whose request contains the file marker.
4. Pass only when the reference is observable on the wire.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "at_ref_expands_in_headless"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    run = next((item for item in observation.runs if item.label == "main"), None)
    if run is None or run.rc != 0 or not run.requests:
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.INCONCLUSIVE,
            "HEADLESS_PROBE_UNOBSERVED",
        )
    passed = any(
        RECIPE.probe_token in request.body or RECIPE.probe_token in request.probe_hits
        for request in run.requests
    )
    return HeadlessJudgement(
        HeadlessOutcome.PASS if passed else HeadlessOutcome.FAIL,
        HeadlessApplicability.APPLICABLE,
        "HEADLESS_PROFILE_CHECKED",
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
