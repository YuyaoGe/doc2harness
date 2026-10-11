"""Require a healthy web-fetch exchange in the control path.

1. Send the control web-fetch request.
2. Capture response and terminal facts.
3. Apply the typed direct-mock judge to the observation.
"""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockApplicability, DirectMockEntrypoint, DirectMockFactKey,
    DirectMockFixtureProfile, DirectMockJudgement, DirectMockMode,
    DirectMockOracleProfile, DirectMockOutcome, DirectMockRawObservation,
    DirectMockRecipe, DirectMockScenario, define_direct_mock_scenario,
)

SCENARIO_ID = "webfetch_http_failure_control"
RECIPE = DirectMockRecipe(
    DirectMockMode.SEQUENTIAL_PROBE, DirectMockEntrypoint.PYTHON_ENTRY,
    fixture_profile=DirectMockFixtureProfile.WEB_FETCH,
    oracle_profile=DirectMockOracleProfile.FACT_CONTRACT,
    max_steps=3, timeout_seconds=45,
    environment=(("HTTP_PROXY", ""), ("HTTPS_PROXY", ""), ("ALL_PROXY", ""),
                 ("NO_PROXY", "127.0.0.1,localhost"),
                 ("no_proxy", "127.0.0.1,localhost")),
    required_facts=(DirectMockFactKey.WEB_FETCH_HEALTHY,),
)
STEPS = (RECIPE,)
SCENARIO: DirectMockScenario = define_direct_mock_scenario(SCENARIO_ID, RECIPE, ScenarioRole.CONTROL)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: DirectMockRawObservation) -> DirectMockJudgement:
    """Require the declared fact and a live, recorded, non-timeout run."""

    observed = {item.key: item.value for item in observation.facts}
    passed = (all(observed.get(key) is True for key in RECIPE.required_facts)
              and bool(observation.requests) and observation.status_written
              and not observation.timed_out)
    return DirectMockJudgement(
        DirectMockOutcome.PASS if passed else DirectMockOutcome.FAIL,
        DirectMockApplicability.APPLICABLE, DirectMockOracleProfile.FACT_CONTRACT.value,
    )


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
