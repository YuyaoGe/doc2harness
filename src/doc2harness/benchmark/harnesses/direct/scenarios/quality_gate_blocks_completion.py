"""Require a failed quality hook to prevent a submitted completion.

1. Run the completion through the failing quality hook.
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

SCENARIO_ID = "quality_gate_blocks_completion"
RECIPE = DirectMockRecipe(
    DirectMockMode.SEQUENTIAL_PROBE,
    DirectMockEntrypoint.PYTHON_ENTRY,
    max_steps=4,
    timeout_seconds=90,
    fixture_profile=DirectMockFixtureProfile.QUALITY_GATE_FAIL,
    oracle_profile=DirectMockOracleProfile.QUALITY_GATE_BLOCKS,
    control_dependency="quality_gate_blocks_completion_control",
)
STEPS = (RECIPE,)
SCENARIO: DirectMockScenario = define_direct_mock_scenario(
    SCENARIO_ID, RECIPE, ScenarioRole.TARGET
)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: DirectMockRawObservation) -> DirectMockJudgement:
    """Require a recorded gate and a non-submitted terminal status."""

    gate_ran = bool(next(
        (item.value for item in observation.facts if item.key is DirectMockFactKey.QGATE_RAN),
        False,
    ))
    if not observation.status_written or not observation.requests or not gate_ran:
        return DirectMockJudgement(
            DirectMockOutcome.PASS,
            DirectMockApplicability.INCONCLUSIVE,
            "QUALITY_GATE_NOT_OBSERVED",
        )
    outcome = DirectMockOutcome.PASS if observation.exit_status != "Submitted" else DirectMockOutcome.FAIL
    return DirectMockJudgement(outcome, DirectMockApplicability.APPLICABLE, DirectMockOracleProfile.QUALITY_GATE_BLOCKS.value)


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
