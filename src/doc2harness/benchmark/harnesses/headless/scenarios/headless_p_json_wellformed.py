"""Require the normal headless control run to emit clean JSON.

1. Launch the control recipe with its fixed JSON response gateway.
2. Capture the main process output and terminal status.
3. Parse the output as one JSON value and look for the control sentinel.
4. Pass only when parsing, sentinel, exit code, and noise constraints hold.
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

SCENARIO_ID = "headless_p_json_wellformed"
SCENARIO: HeadlessScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
# This control uses the fixed-reply recipe without additional scenario-specific
# setup; retaining the complete recipe as the declarative step preserves every
# recipe default and makes the selected gateway visible in this file.
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: HeadlessObservation) -> HeadlessJudgement:
    """Check the fixed JSON control response without reading host state."""

    main = next((run for run in observation.runs if run.label == "main"), None)
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
    if main is None:
        return HeadlessJudgement(
            HeadlessOutcome.FAIL,
            HeadlessApplicability.INCONCLUSIVE,
            "HEADLESS_MAIN_MISSING",
        )
    try:
        value = json.loads(main.stdout.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        value = None
    passed = (
        value is not None
        and RECIPE.oracle.sentinel in json.dumps(value, ensure_ascii=False)
        and main.rc == 0
        and not any(
            marker in main.stdout
            for marker in ("<function_calls>", "<tool_code>", "<invoke", "<response>", "TurnBegin", "StepBegin", "Traceback (most recent call)")
        )
    )
    return HeadlessJudgement(
        HeadlessOutcome.PASS if passed else HeadlessOutcome.FAIL,
        HeadlessApplicability.APPLICABLE,
        "HEADLESS_PROFILE_CHECKED",
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
