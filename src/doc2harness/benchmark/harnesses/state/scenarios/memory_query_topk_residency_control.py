"""Verify the control route for a bounded memory top-k query.

1. Run the small control memory fixture with a bounded top-k query.
2. Capture the scenario-specific structured-facts row.
3. Require observed/support evidence and an all-true residency check vector.
4. Return PASS only when the control query is complete and cleanup is clean.
"""

from doc2harness.benchmark.harnesses.state.contracts import (
    StateApplicability,
    StateEvaluation,
    StateObservation,
    StateOracleProfile,
    StateOutcome,
    StateScenario,
    compile_state_steps,
    evaluation_digest,
    structured_fact,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "memory_query_topk_residency_control"
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_VARIANTS_KEY = "clipboard_api_variants"
_EXECUTION_PARAMETERS = (
    ("kind", "control"),
    (_VARIANTS_KEY, ((
        ("decoy", "DECOY_"),
        ("entries", 2000),
        ("label", "control"),
        ("markers", ("MEMORY_ALPHA_6402", "MEMORY_BETA_6403", "MEMORY_GAMMA_6404")),
        ("operation", "memory"),
        ("rss_probe_bytes", 16777216),
        ("stub", (("mode", "seq_probe"), ("probe_tokens", ("MEMORY_TARGET_ROUTE_6406",)), ("rounds", (
            ((("args", (("limit", 3), ("query", "zirconium quasar heliotrope"))), ("name", ("memory_search", "memory-search", "memorysearch"))),),
            ((("name", "__finish__"),),),
        )))),
        ("target_prompt", "MEMORY_TARGET_ROUTE_6406"),
        ("warm_query", "warmup_only_6401"),
    ),),),
)
STEPS = (structured_fact("structured_facts"),)
RECIPE = compile_state_steps(
    STEPS,
    oracle_profile=StateOracleProfile.STRUCTURED_FACTS,
    oracle_parameters=(("fact_key", SCENARIO_ID),),
    execution_parameters=_EXECUTION_PARAMETERS,
    timeout_seconds=120,
)
SCENARIO: StateScenario = StateScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = _BASE_SCENARIO
definition = SCENARIO.definition


def judge(observation: StateObservation) -> StateEvaluation:
    """Apply the structured memory-residency control contract using typed facts only."""
    if observation.scenario_id != SCENARIO_ID:
        raise ValueError("state observation belongs to another scenario")
    facts = dict(observation.facts)
    passed, conclusive, reason = False, True, "CHILD_RESULT_MISSING"
    if not observation.fixture_verified:
        conclusive, reason = False, "FIXTURE_UNVERIFIED"
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
                public_calls = row.get("public_calls")
                if type(public_calls) is not bool:
                    conclusive, reason = False, "STRUCTURED_PUBLIC_CALLS_UNOBSERVED"
                elif public_calls is not True:
                    reason = "STRUCTURED_PUBLIC_CALLS_FAILED"
                else:
                    passed, reason = True, "STRUCTURED_FACTS_CHECKED"
    if conclusive and (not observation.process_tree_clean or not observation.workspace_clean):
        passed, reason = False, "EXECUTION_CLEANUP_FAILED"
    return StateEvaluation(SCENARIO_ID, StateOutcome.PASS if passed else StateOutcome.FAIL, StateApplicability.APPLICABLE if conclusive else StateApplicability.INCONCLUSIVE, reason, evaluation_digest(SCENARIO, observation), SCENARIO.definition.weight_microunits if conclusive else 0)


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
