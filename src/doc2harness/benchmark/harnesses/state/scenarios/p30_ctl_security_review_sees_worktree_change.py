"""Verify that the security-review control route sees a worktree change.

1. Seed the scenario-specific fixture files and invoke the command sequence.
2. Capture the model request facts after the scenario's execution gates.
3. Check whether the expected token appears in a request probe.
4. Return PASS only when the request is observed and cleanup is clean.
"""

from doc2harness.benchmark.harnesses.state.contracts import (
    StateApplicability,
    StateEvaluation,
    StateObservation,
    StateOracleProfile,
    StateOutcome,
    StateScenario,
    command,
    compile_state_steps,
    evaluation_digest,
    observe_requests,
    seed_file,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "p30_ctl_security_review_sees_worktree_change"
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
STEPS = (
    seed_file("vuln_9601.py", "import os\n"),
    command("/security-review"),
    observe_requests(),
)
RECIPE = compile_state_steps(
    STEPS,
    oracle_profile=StateOracleProfile.REQUEST_TOKEN_PRESENT,
    oracle_parameters=_BASE_SCENARIO.recipe.oracle_parameters,
    execution_parameters=_BASE_SCENARIO.recipe.execution_parameters,
    dependencies=_BASE_SCENARIO.recipe.dependencies,
    timeout_seconds=_BASE_SCENARIO.recipe.timeout_seconds,
)
SCENARIO: StateScenario = StateScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = _BASE_SCENARIO
definition = SCENARIO.definition


def _mapping(value: object) -> dict[str, object]:
    if type(value) is tuple and all(
        type(item) is tuple and len(item) == 2 and type(item[0]) is str
        for item in value
    ):
        return dict(value)
    return {}


def _requests(value: object) -> tuple[dict[str, object], ...]:
    if type(value) is not tuple:
        return ()
    return tuple(_mapping(item) for item in value if type(item) is tuple)


def _probe(request: dict[str, object], token: str) -> bool:
    return _mapping(request.get("probes")).get(token) is True


def judge(observation: StateObservation) -> StateEvaluation:
    """Apply the REQUEST_TOKEN_PRESENT contract using typed request facts."""
    if observation.scenario_id != SCENARIO_ID:
        raise ValueError("state observation belongs to another scenario")
    facts = _mapping(observation.facts)
    requests = _requests(facts.get("requests"))
    params = _mapping(RECIPE.oracle_parameters)
    token = params.get("token") if type(params.get("token")) is str else ""
    passed, conclusive, reason = False, True, "REQUEST_TOKEN_CHECKED"
    if not requests:
        conclusive, reason = False, "MODEL_TURN_UNOBSERVED"
    else:
        passed = any(_probe(request, token) for request in requests)
    if conclusive and (
        not observation.process_tree_clean or not observation.workspace_clean
    ):
        passed, reason = False, "EXECUTION_CLEANUP_FAILED"
    return StateEvaluation(
        SCENARIO_ID,
        StateOutcome.PASS if passed else StateOutcome.FAIL,
        StateApplicability.APPLICABLE
        if conclusive
        else StateApplicability.INCONCLUSIVE,
        reason,
        evaluation_digest(SCENARIO, observation),
        SCENARIO.definition.weight_microunits if conclusive else 0,
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
