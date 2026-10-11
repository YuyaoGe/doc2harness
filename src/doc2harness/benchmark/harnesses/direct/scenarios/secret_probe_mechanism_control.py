"""Control: prove the secret probe mechanism is observable when requested.

1. Request the control secret probe explicitly.
2. Capture probe and terminal facts.
3. Apply the typed direct-mock judge to the observation.
"""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockApplicability, DirectMockEntrypoint, DirectMockFixtureProfile,
    DirectMockJudgement, DirectMockMode, DirectMockOracleProfile,
    DirectMockOutcome, DirectMockRawObservation, DirectMockRecipe,
    DirectMockScenario, define_direct_mock_scenario,
)

SCENARIO_ID = "secret_probe_mechanism_control"
RECIPE = DirectMockRecipe(
    DirectMockMode.SEQUENTIAL_PROBE, DirectMockEntrypoint.PYTHON_ENTRY, max_steps=2, timeout_seconds=180,
    fixture_profile=DirectMockFixtureProfile.SECRET_CONTROL,
    oracle_profile=DirectMockOracleProfile.PROBE_PRESENT,
)
STEPS = (RECIPE,)
SCENARIO: DirectMockScenario = define_direct_mock_scenario(SCENARIO_ID, RECIPE, ScenarioRole.CONTROL)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: DirectMockRawObservation) -> DirectMockJudgement:
    """Pass only when a live clean request carries a probe marker."""

    clean = observation.exit_status in {"Submitted", "LimitsExceeded"}
    passed = bool(observation.requests) and any(item.probe_seen for item in observation.requests) and clean
    return DirectMockJudgement(DirectMockOutcome.PASS if passed else DirectMockOutcome.FAIL, DirectMockApplicability.APPLICABLE, DirectMockOracleProfile.PROBE_PRESENT.value)


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
