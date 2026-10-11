"""Require a model-error headless run to remain a valid JSON error.

1. Launch the target recipe with the configured model-error gateway.
2. Capture the main process output and at least one model request.
3. Parse the response as JSON without relying on human-readable logs.
4. Pass only when the JSON is valid and the process exits with code one.
"""

import json

from doc2harness.benchmark.harnesses.headless.contracts import (
    HeadlessApplicability,
    HeadlessJudgement,
    HeadlessObservation,
    HeadlessOutcome,
    HeadlessScenario,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "headless_p_json_model_error_wellformed"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    """Require a live request and a parseable non-success response."""

    main = next((run for run in observation.runs if run.label == "main"), None)
    requests = tuple(request for run in observation.runs for request in run.requests)
    if not observation.cli_available:
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.INCONCLUSIVE,
            "HEADLESS_CLI_UNAVAILABLE",
        )
    if observation.driver_error:
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.APPLICABLE,
            "HEADLESS_DRIVER_ERROR",
        )
    if main is None or not requests:
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.INCONCLUSIVE,
            "HEADLESS_ERROR_PATH_UNOBSERVED",
        )
    try:
        value = json.loads(main.stdout.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        value = None
    passed = value is not None and main.rc == 1
    return HeadlessJudgement(
        HeadlessOutcome.PASS if passed else HeadlessOutcome.FAIL,
        HeadlessApplicability.APPLICABLE,
        "HEADLESS_PROFILE_CHECKED",
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
