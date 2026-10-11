"""Verify the structured close-retry evidence for the target route.

1. Run the close operation through the session-close API variant fixture.
2. Capture the scenario-specific structured-facts row.
3. Require observed/support evidence and an all-true behavioral check vector.
4. Return PASS only when the structured route is complete and cleanup is clean.
"""

from doc2harness.benchmark.harnesses.state.contracts import (
    StateApplicability, StateEvaluation, StateObservation, StateOracleProfile,
    StateOutcome, StateScenario, compile_state_steps,
    evaluation_digest, structured_fact,
)
from doc2harness.benchmark.definition import DependencyKind
from ..recipes import scenario_by_id

SCENARIO_ID = "agent_api_close_retry_incomplete"
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_VARIANTS_KEY = "clipboard_api_variants"
_EXEC = (
        (_VARIANTS_KEY, ((
        ("heartbeat_file", "close_helper.heartbeat"), ("inject_fault", True), ("label", "target"),
        ("lease_file", "close_helper.lease"), ("operation", "close"), ("pid_file", "close_helper.pid"),
        ("prompt", "CLOSE_ROUTE_6501"), ("state_file", "close_helper.state"),
        ("stub", (
            ("mode", "seq_probe"), ("probe_tokens", ("CLOSE_ROUTE_6501",)),
            ("rounds", (
                ((("args", (("run_in_background", True),)), ("command", "__CLOSE_COMMAND__"), ("name", "bash")),),
                ((("name", "__finish__"),),),
            )),
        )),
    ),)) ,
)
STEPS = (structured_fact("structured_facts"),)
RECIPE = compile_state_steps(
    STEPS, oracle_profile=StateOracleProfile.STRUCTURED_FACTS,
    oracle_parameters=(("fact_key", SCENARIO_ID),), execution_parameters=_EXEC,
    dependencies=(("agent_api_close_retry_incomplete_control", DependencyKind.GATE),),
    timeout_seconds=45,
)
SCENARIO: StateScenario = StateScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = _BASE_SCENARIO
definition = SCENARIO.definition


def judge(observation: StateObservation) -> StateEvaluation:
    """Apply the structured-facts close-retry contract using typed facts only."""
    if observation.scenario_id != SCENARIO_ID:
        raise ValueError("state observation belongs to another scenario")
    facts = dict(observation.facts)
    passed, conclusive, reason = False, True, "CHILD_RESULT_MISSING"
    if not observation.fixture_verified:
        conclusive, reason = False, "FIXTURE_UNVERIFIED"
    elif facts.get("result_present") is not True:
        reason = "CHILD_RESULT_MISSING"
    else:
        structured = facts.get("structured_facts")
        structured = dict(structured) if type(structured) is tuple else {}
        row = structured.get(SCENARIO_ID)
        row = dict(row) if type(row) is tuple else {}
        if row.get("observed") is not True or row.get("supported") is False:
            conclusive, reason = False, "STRUCTURED_FACTS_UNOBSERVED"
        else:
            checks = row.get("checks")
            checks = checks if type(checks) is tuple else ()
            if not checks:
                conclusive, reason = False, "STRUCTURED_CHECKS_MISSING"
            elif not all(item is True for item in checks):
                reason = "STRUCTURED_FACTS_FAILED"
            else:
                passed, reason = True, "STRUCTURED_FACTS_CHECKED"
    if conclusive and (not observation.process_tree_clean or not observation.workspace_clean):
        passed, reason = False, "EXECUTION_CLEANUP_FAILED"
    return StateEvaluation(
        SCENARIO_ID, StateOutcome.PASS if passed else StateOutcome.FAIL,
        StateApplicability.APPLICABLE if conclusive else StateApplicability.INCONCLUSIVE,
        reason, evaluation_digest(SCENARIO, observation),
        SCENARIO.definition.weight_microunits if conclusive else 0,
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
