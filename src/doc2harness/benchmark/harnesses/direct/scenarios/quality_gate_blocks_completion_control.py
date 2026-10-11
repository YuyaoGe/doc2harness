"""Require a successful quality hook to permit a submitted completion.

1. Run the completion through the successful quality hook.
2. Capture quality, submission, and terminal facts.
3. Apply the typed direct-mock judge to the observation.
"""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockApplicability,
    DirectMockEntrypoint,
    DirectMockFactKey,
    DirectMockFixtureProfile,
    DirectMockJudgement,
    DirectMockMode,
    DirectMockOracleProfile,
    DirectMockOutcome,
    DirectMockRawObservation,
    DirectMockRecipe,
    DirectMockScenario,
    define_direct_mock_scenario,
)

SCENARIO_ID = "quality_gate_blocks_completion_control"
RECIPE = DirectMockRecipe(
    DirectMockMode.SEQUENTIAL_PROBE,
    DirectMockEntrypoint.PYTHON_ENTRY,
    max_steps=4,
    timeout_seconds=90,
    fixture_profile=DirectMockFixtureProfile.QUALITY_GATE_PASS,
    oracle_profile=DirectMockOracleProfile.QUALITY_GATE_ALLOWS_CONTROL,
)
STEPS = (RECIPE,)
SCENARIO: DirectMockScenario = define_direct_mock_scenario(
    SCENARIO_ID, RECIPE, ScenarioRole.CONTROL
)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: DirectMockRawObservation) -> DirectMockJudgement:
    """Require the gate marker, a submitted status, and zero exit code."""

    gate_ran = bool(next(
        (item.value for item in observation.facts if item.key is DirectMockFactKey.QGATE_RAN),
        False,
    ))
    if not gate_ran:
        return DirectMockJudgement(
            DirectMockOutcome.PASS,
            DirectMockApplicability.INCONCLUSIVE,
            "QUALITY_GATE_NOT_OBSERVED",
        )
    passed = observation.exit_status == "Submitted" and observation.exit_code == 0
    return DirectMockJudgement(
        DirectMockOutcome.PASS if passed else DirectMockOutcome.FAIL,
        DirectMockApplicability.APPLICABLE,
        DirectMockOracleProfile.QUALITY_GATE_ALLOWS_CONTROL.value,
    )


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
