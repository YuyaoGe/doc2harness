"""Verify that the target route preserves UTF-8 clipboard bytes.

1. Run the UTF-8 copy operation through the target API variant after its control gate.
2. Capture the scenario-specific structured-facts row.
3. Require observed/support evidence and an all-true behavioral check vector.
4. Return PASS only when the structured route is complete and cleanup is clean.
"""

from doc2harness.benchmark.definition import DependencyKind
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

SCENARIO_ID = "copy_utf8_clipboard_bytes"
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_VARIANTS_KEY = "clipboard_api_variants"
_EXECUTION_PARAMETERS = (
    (
        _VARIANTS_KEY,
        ((
            ("capture", "clipboard_capture.bin"),
            ("label", "target"),
            ("meta", "clipboard_meta.json"),
            ("operation", "copy"),
            ("prompt", "COPY_ROUTE_6601"),
            ("stub", (
                ("mode", "fixed_reply"),
                ("probe_tokens", ("COPY_ROUTE_6601",)),
                ("reply_content", "COPY_UTF8_6603 雪豹 café Δ"),
            )),
        ),),
    ),
)
STEPS = (structured_fact("structured_facts"),)
RECIPE = compile_state_steps(
    STEPS,
    oracle_profile=StateOracleProfile.STRUCTURED_FACTS,
    oracle_parameters=(("fact_key", SCENARIO_ID),),
    execution_parameters=_EXECUTION_PARAMETERS,
    dependencies=(("copy_utf8_clipboard_control", DependencyKind.GATE),),
    timeout_seconds=45,
)
SCENARIO: StateScenario = StateScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = _BASE_SCENARIO
definition = SCENARIO.definition


def judge(observation: StateObservation) -> StateEvaluation:
    """Apply the structured UTF-8 clipboard contract using typed facts only."""
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
        SCENARIO_ID,
        StateOutcome.PASS if passed else StateOutcome.FAIL,
        StateApplicability.APPLICABLE if conclusive else StateApplicability.INCONCLUSIVE,
        reason,
        evaluation_digest(SCENARIO, observation),
        SCENARIO.definition.weight_microunits if conclusive else 0,
    )

__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
