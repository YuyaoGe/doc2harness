"""Verify that a healthy model route returns the canonical reply.

1. Run the model-success formal contract probe.
2. Capture request count, stdout, return code, and exit status.
3. Require two requests and the exact submitted reply.
4. Return PASS only when the control route is fully healthy.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "runtime_error_stdout_control"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    """Check the healthy model control response."""

    labels = tuple(probe.label for probe in RECIPE.formal_contract_probes)
    by_label = {run.label: run for run in observation.runs}
    runs = tuple(by_label.get(label) for label in labels)
    if any(run is None or run.start_error or run.timed_out or not run.cleanup_complete for run in runs):
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "HEADLESS_CONTRACT_MATRIX_INCOMPLETE")
    passed = all(
        run is not None
        and run.rc == 0
        and len(run.requests) == 2
        and run.stdout == "EXACT_REPLY_6047\n"
        and run.exit_status == "Submitted"
        for run in runs
    )
    return HeadlessJudgement(HeadlessOutcome.PASS if passed else HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "HEADLESS_PROFILE_CHECKED")


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
