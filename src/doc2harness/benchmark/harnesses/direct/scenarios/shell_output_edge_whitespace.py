"""Require target-path shell bytes to remain intact at whitespace edges.

1. Send the shell command with edge whitespace.
2. Capture exact output bytes and terminal facts.
3. Apply the typed direct-mock judge to the observation.
"""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.direct.contracts import (
    DirectMockApplicability, DirectMockEntrypoint, DirectMockFactKey,
    DirectMockFixtureProfile, DirectMockJudgement, DirectMockMode,
    DirectMockOracleProfile, DirectMockOutcome, DirectMockRawObservation,
    DirectMockRecipe, DirectMockScenario, define_direct_mock_scenario,
)

SCENARIO_ID = "shell_output_edge_whitespace"
RECIPE = DirectMockRecipe(
    DirectMockMode.SEQUENTIAL_PROBE, DirectMockEntrypoint.PYTHON_ENTRY,
    fixture_profile=DirectMockFixtureProfile.SHELL_EDGE_TARGET,
    oracle_profile=DirectMockOracleProfile.FACT_CONTRACT,
    max_steps=3, timeout_seconds=45,
    required_facts=(DirectMockFactKey.SHELL_EDGE_BYTES,),
    control_dependencies=("shell_output_edge_whitespace_control",),
)
STEPS = (RECIPE,)
SCENARIO: DirectMockScenario = define_direct_mock_scenario(SCENARIO_ID, RECIPE, ScenarioRole.TARGET)
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
