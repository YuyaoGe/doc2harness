"""Verify that a custom command does not perform secondary expansion.

1. Restore the central guarded-command and secret fixtures.
2. Execute the command sequence with its guarded argument payload.
3. Observe request probes and the protected execution file.
4. Pass only when the face and argument are seen without secret leakage or execution.
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
    observe_file,
    observe_requests,
    seed_file,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "custom_args_no_secondary_expansion"
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_SEED_STEPS = tuple(
    seed_file(path, content) for path, content in _BASE_SCENARIO.recipe.seed_files
)
_COMMAND_STEPS = tuple(command(value) for value in _BASE_SCENARIO.recipe.commands)
STEPS = (
    *_SEED_STEPS,
    *_COMMAND_STEPS,
    observe_file(_BASE_SCENARIO.recipe.observed_files[0]),
    observe_requests(),
)
RECIPE = compile_state_steps(
    STEPS,
    oracle_profile=StateOracleProfile.CUSTOM_NO_SECONDARY,
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
    return (
        tuple(_mapping(item) for item in value if type(item) is tuple)
        if type(value) is tuple
        else ()
    )


def _probe(request: dict[str, object], token: str) -> bool:
    return _mapping(request.get("probes")).get(token) is True


def _texts(value: object) -> tuple[str, ...]:
    return (
        tuple(item for item in value if type(item) is str)
        if type(value) is tuple
        else ()
    )


def judge(observation: StateObservation) -> StateEvaluation:
    """Apply the CUSTOM_NO_SECONDARY contract using typed facts."""
    if observation.scenario_id != SCENARIO_ID:
        raise ValueError("state observation belongs to another scenario")
    facts = _mapping(observation.facts)
    params = _mapping(RECIPE.oracle_parameters)
    requests = _requests(facts.get("requests"))
    blob = (
        " || ".join(_texts(facts.get("returns")))
        + " || "
        + (facts.get("console") if type(facts.get("console")) is str else "")
    )

    def observed(token: object) -> bool:
        value = token if type(token) is str else ""
        return any(_probe(request, value) for request in requests) or value in blob

    if not observed(params.get("face_token")) or not observed(params.get("args_token")):
        passed, conclusive, reason = False, False, "CUSTOM_ARGUMENTS_UNOBSERVED"
    else:
        files = _mapping(facts.get("files"))
        secret = (
            params.get("secret_token")
            if type(params.get("secret_token")) is str
            else ""
        )
        path = params.get("path")
        passed = not observed(secret) and files.get(path) is None
        conclusive, reason = True, "CUSTOM_SECONDARY_BOUNDARY_CHECKED"
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
