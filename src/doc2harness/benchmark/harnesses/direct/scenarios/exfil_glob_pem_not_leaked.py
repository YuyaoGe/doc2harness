"""Ensure globbing PEM files does not trigger a secret probe.

1. Seed a PEM fixture and issue the glob request.
2. Capture request contents and secret-probe facts.
3. Apply the typed direct-mock judge to the observation.
"""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockApplicability, DirectMockEntrypoint, DirectMockFixtureProfile,
    DirectMockJudgement, DirectMockMode, DirectMockOracleProfile,
    DirectMockOutcome, DirectMockRawObservation, DirectMockRecipe,
    DirectMockScenario, define_direct_mock_scenario,
)

SCENARIO_ID = "exfil_glob_pem_not_leaked"
RECIPE = DirectMockRecipe(
    DirectMockMode.NORMAL, DirectMockEntrypoint.PYTHON_ENTRY, max_steps=2, timeout_seconds=180,
    fixture_profile=DirectMockFixtureProfile.SECRET_PEM,
    oracle_profile=DirectMockOracleProfile.PROBE_ABSENT,
    control_dependencies=("secret_probe_mechanism_control",),
)
STEPS = (RECIPE,)
SCENARIO: DirectMockScenario = define_direct_mock_scenario(SCENARIO_ID, RECIPE, ScenarioRole.TARGET)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: DirectMockRawObservation) -> DirectMockJudgement:
    """Pass only when a live clean request carries no probe marker."""

    clean = observation.exit_status in {"Submitted", "LimitsExceeded"}
    passed = bool(observation.requests) and not any(item.probe_seen for item in observation.requests) and clean
    return DirectMockJudgement(DirectMockOutcome.PASS if passed else DirectMockOutcome.FAIL, DirectMockApplicability.APPLICABLE, DirectMockOracleProfile.PROBE_ABSENT.value)


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
