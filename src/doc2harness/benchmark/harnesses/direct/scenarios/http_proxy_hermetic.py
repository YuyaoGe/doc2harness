"""Require explicit endpoints to bypass the configured proxy.

1. Send the explicit-endpoint request with a configured proxy.
2. Capture endpoint routing and terminal facts.
3. Apply the typed direct-mock judge to the observation.
"""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockApplicability, DirectMockEntrypoint, DirectMockFactKey,
    DirectMockFixtureProfile, DirectMockJudgement, DirectMockMode,
    DirectMockOracleProfile, DirectMockOutcome, DirectMockRawObservation,
    DirectMockRecipe, DirectMockScenario, define_direct_mock_scenario,
)

SCENARIO_ID = "http_proxy_hermetic"
RECIPE = DirectMockRecipe(
    DirectMockMode.FIXED_REPLY, DirectMockEntrypoint.PYTHON_ENTRY,
    fixture_profile=DirectMockFixtureProfile.HTTP_PROXY,
    oracle_profile=DirectMockOracleProfile.FACT_CONTRACT,
    max_steps=2, timeout_seconds=30,
    required_facts=(DirectMockFactKey.PROXY_SELF_CHECKED,
                    DirectMockFactKey.EXPLICIT_ENDPOINT_BYPASSED_PROXY),
    control_dependencies=("http_proxy_hermetic_control",),
)
STEPS = (RECIPE,)
SCENARIO: DirectMockScenario = define_direct_mock_scenario(SCENARIO_ID, RECIPE, ScenarioRole.TARGET)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: DirectMockRawObservation) -> DirectMockJudgement:
    """Require both proxy facts and a live, recorded, non-timeout run."""

    observed = {item.key: item.value for item in observation.facts}
    passed = (all(observed.get(key) is True for key in RECIPE.required_facts)
              and bool(observation.requests) and observation.status_written
              and not observation.timed_out)
    return DirectMockJudgement(
        DirectMockOutcome.PASS if passed else DirectMockOutcome.FAIL,
        DirectMockApplicability.APPLICABLE, DirectMockOracleProfile.FACT_CONTRACT.value,
    )


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
