"""Verify that valid budget values are accepted by the headless runtime.

1. Run the valid-zero and valid-positive budget probes.
2. Capture exit status, request count, and submission status for each probe.
3. Require one request and a successful submitted/limited result per probe.
4. Return PASS only when the complete valid-budget control matrix is observed.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "budget_type_domain_control"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    """Check the valid budget probes without evaluating invalid input variants."""

    labels = tuple(probe.label for probe in RECIPE.formal_runtime_probes)
    by_label = {run.label: run for run in observation.runs}
    runs = tuple(by_label.get(label) for label in labels)
    if any(
        run is None
        or run.start_error
        or run.timed_out
        or not run.cleanup_complete
        or not run.exit_status
        for run in runs
    ):
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.APPLICABLE,
            "HEADLESS_FORMAL_MATRIX_INCOMPLETE",
        )
    passed = all(
        run is not None
        and run.rc == 0
        and run.exit_status in {"Submitted", "LimitsExceeded"}
        and len(run.requests) == 1
        for run in runs
    )
    return HeadlessJudgement(
        HeadlessOutcome.PASS if passed else HeadlessOutcome.FAIL,
        HeadlessApplicability.APPLICABLE,
        "HEADLESS_PROFILE_CHECKED",
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
