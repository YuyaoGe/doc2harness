"""Verify the task text reaches the model request wire.

1. Run the task containing the explicit reference text.
2. Observe the main process and request records.
3. Require the planted task marker in a request.
4. Pass only when the marker is transmitted.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "p34_ctl_task_text_reaches_wire"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    run = next((r for r in observation.runs if r.label == "main"), None)
    if run is None or not run.requests:
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
        HeadlessOutcome.PASS if present else HeadlessOutcome.FAIL,
        HeadlessApplicability.APPLICABLE,
        "HEADLESS_PROFILE_CHECKED",
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
