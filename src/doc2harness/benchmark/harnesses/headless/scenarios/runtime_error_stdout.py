"""Verify that a model runtime error is surfaced without fake stdout.

1. Run the model-runtime-error formal contract probe.
2. Capture request count, stdout, return code, and exit status.
3. Require one request, rc 1, empty stdout, and Error status.
4. Return PASS only when the runtime error remains honest.
"""

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "runtime_error_stdout"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    """Check honest runtime-error evidence for the target route."""

    labels = tuple(probe.label for probe in RECIPE.formal_contract_probes)
    by_label = {run.label: run for run in observation.runs}
    runs = tuple(by_label.get(label) for label in labels)
    if any(run is None or run.start_error or run.timed_out or not run.cleanup_complete for run in runs):
        return HeadlessJudgement(HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "HEADLESS_CONTRACT_MATRIX_INCOMPLETE")
    passed = all(
        run is not None
        and run.rc == 1
        and len(run.requests) == 1
        and run.stdout == ""
        and run.exit_status == "Error"
        for run in runs
    )
    return HeadlessJudgement(HeadlessOutcome.PASS if passed else HeadlessOutcome.FAIL, HeadlessApplicability.APPLICABLE, "HEADLESS_PROFILE_CHECKED")


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
