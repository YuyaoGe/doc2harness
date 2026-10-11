"""Verify target child add-directory inheritance and its empty variant.

1. Run target child-grant and child-empty API variants with controlled read probes.
2. Capture the scenario-specific structured-facts row after the control gate.
3. Require observed/support evidence and an all-true behavioral check vector.
4. Return PASS only when inheritance boundaries are conclusive and cleanup is clean.
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

SCENARIO_ID = "child_add_dir_inheritance"
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_VARIANTS_KEY = "api_probe_variants"
_EXECUTION_PARAMETERS = (
    (
        "stub",
        (
            ("mode", "seq_probe"),
            ("probe_tokens", ("CHILD_TARGET_7325", "CHILD_EMPTY_7326")),
            (
                "rounds",
                (
                    (
                        (("args", (("file_path", "__REPO__/main.txt"),)), ("name", "read")),
                        (("args", (("file_path", "__EXTRA__/extra.txt"),)), ("name", "read")),
                        (("args", (("file_path", "__UNAUTHORIZED__/outside.txt"),)), ("name", "read")),
                    ),
                    ((("name", "__finish__"),),),
                ),
            ),
        ),
    ),
    (
        _VARIANTS_KEY,
        (
            (
                ("actor", "child"), ("add_dir", True), ("extra_token", "EXTRA_7322"),
                ("label", "child-target"), ("main_token", "MAIN_7321"),
                ("operation", "child"), ("prompt", "CHILD_TARGET_7325"),
                ("settle_s", "2.0"), ("shape", "child-target"),
                ("unauthorized_token", "UNAUTH_7323"),
            ),
            (
                ("actor", "child"), ("add_dir", False), ("extra_token", "EXTRA_7322"),
                ("label", "child-empty-target"), ("main_token", "MAIN_7321"),
                ("operation", "child"), ("prompt", "CHILD_EMPTY_7326"),
                ("settle_s", "2.0"), ("shape", "child-empty-target"),
                ("unauthorized_token", "UNAUTH_7323"),
            ),
        ),
    ),
)
STEPS = (structured_fact("structured_facts"),)
RECIPE = compile_state_steps(
    STEPS,
    oracle_profile=StateOracleProfile.STRUCTURED_FACTS,
    oracle_parameters=(("fact_key", SCENARIO_ID),),
    execution_parameters=_EXECUTION_PARAMETERS,
    dependencies=(("child_add_dir_inheritance_control", DependencyKind.GATE),),
    timeout_seconds=30,
)
SCENARIO: StateScenario = StateScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = _BASE_SCENARIO
definition = SCENARIO.definition


def judge(observation: StateObservation) -> StateEvaluation:
    """Apply the structured child-inheritance target contract using typed facts only."""
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
