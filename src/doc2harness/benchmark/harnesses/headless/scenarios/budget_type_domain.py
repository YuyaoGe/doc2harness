"""Verify that invalid budget types and values are rejected before execution.

1. Run valid budget probes followed by boolean, string, float, and negative probes.
2. Capture exit status, request count, and submission status for each variant.
3. Require valid probes to submit normally with one request.
4. Require every invalid probe to fail without a request or accepted status.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "budget_type_domain"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    """Check valid budgets and fail-closed behavior for every invalid variant."""

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
    valid = runs[:2]
    invalid = runs[2:]
    valid_ok = all(
        run is not None
        and run.rc == 0
        and run.exit_status in {"Submitted", "LimitsExceeded"}
        and len(run.requests) == 1
        for run in valid
    )
    invalid_ok = all(
        run is not None
        and run.rc not in (None, 0)
        and run.exit_status not in {"Submitted", "LimitsExceeded"}
        and not run.requests
        for run in invalid
    )
    return HeadlessJudgement(
        HeadlessOutcome.PASS if valid_ok and invalid_ok else HeadlessOutcome.FAIL,
        HeadlessApplicability.APPLICABLE,
        "HEADLESS_PROFILE_CHECKED",
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
