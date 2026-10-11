"""Verify that a plugin-declared command face reaches the model wire.

1. Seed the plugin manifest and its command file, then invoke `/probecmd`.
2. Send the continuation prompt through the same session.
3. Capture the recorder-owned command-face request probe after the gate.
4. Pass only when the plugin token reaches a model request without an unknown-command result.
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

SCENARIO_ID = "plugin_declared_command_reaches_wire"
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
STEPS = (
    seed_file("plugins/probeplug/commands/probecmd.md", "Summarize PLUGCMD_5150 for the team now.\n"),
    seed_file("plugins/probeplug/plugin.json", '{\n "name": "probeplug",\n "description": "probe plugin",\n "commands": [\n  "commands/probecmd.md"\n ]\n}'),
    command("/probecmd"),
    command("please continue"),
    observe_requests(),
)
RECIPE = compile_state_steps(STEPS, oracle_profile=StateOracleProfile.CUSTOM_COMMAND_FACE,
    oracle_parameters=_BASE_SCENARIO.recipe.oracle_parameters,
    execution_parameters=_BASE_SCENARIO.recipe.execution_parameters,
    dependencies=_BASE_SCENARIO.recipe.dependencies, timeout_seconds=_BASE_SCENARIO.recipe.timeout_seconds)
SCENARIO: StateScenario = StateScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = _BASE_SCENARIO
definition = SCENARIO.definition


def _probe(request: dict[str, object], token: str) -> bool:
    probes = request.get("probes")
    return type(probes) is tuple and dict(probes).get(token) is True


def judge(observation: StateObservation) -> StateEvaluation:
    """Apply the custom-command face contract using typed request facts only."""
    if observation.scenario_id != SCENARIO_ID:
        raise ValueError("state observation belongs to another scenario")
    facts = dict(observation.facts)
    requests = facts.get("requests")
    requests = requests if type(requests) is tuple else ()
    requests = tuple(dict(item) for item in requests if type(item) is tuple)
    returns = facts.get("returns")
    returns = returns if type(returns) is tuple else ()
    params = dict(RECIPE.oracle_parameters)
    passed, conclusive, reason = False, True, "CUSTOM_COMMAND_FACE_CHECKED"
    if not observation.fixture_verified:
        conclusive, reason = False, "FIXTURE_UNVERIFIED"
    elif returns and any("unknown command" in str(item).lower() for item in returns):
        reason = "CUSTOM_COMMAND_MISSING"
    elif not requests:
        conclusive, reason = False, "MODEL_TURN_UNOBSERVED"
    else:
        passed = any(_probe(request, params["token"]) for request in requests)
    if conclusive and (not observation.process_tree_clean or not observation.workspace_clean):
        passed, reason = False, "EXECUTION_CLEANUP_FAILED"
    return StateEvaluation(SCENARIO_ID, StateOutcome.PASS if passed else StateOutcome.FAIL,
        StateApplicability.APPLICABLE if conclusive else StateApplicability.INCONCLUSIVE,
        reason, evaluation_digest(SCENARIO, observation), SCENARIO.definition.weight_microunits if conclusive else 0)


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
