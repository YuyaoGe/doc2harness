"""Verify undo restores the original file after a full overwrite.
1. Seed the target, enable yolo mode, read it beside a live Bash probe, and overwrite it.
2. Issue undo at the session boundary.
3. Run a post-undo probe and capture the resulting transition facts.
4. Pass when the overwrite snapshot and both restored snapshots match exactly and execution is clean.
"""

from dataclasses import replace

from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolApplicability,
    FileToolJudgement,
    FileToolOracleProfile,
    FileToolOutcome,
    FileToolRawObservation,
    FileToolScenario,
    build_file_tool_evaluation,
    capture_fact,
    compile_file_tool_steps,
    setup_command,
    tool_round,
    validate_file_tool_observation,
    write_fixture,
)
from ..recipes import scenario_by_id

SCENARIO_ID = "undo_reverts_overwrite_control"
STEPS = (
    write_fixture("tgt.txt", "ORIG_PERM_0001\n"),
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round("read", turn=1, arguments_nested=True, file_path="tgt.txt"),
    tool_round(
        "write",
        turn=2,
        arguments_nested=True,
        file_path="tgt.txt",
        content="OVR_BODY_7755\n",
    ),
    tool_round("bash", turn=3, command="echo POSTUNDO_7755"),
    capture_fact("target_content", required=(), min_requests=2),
)
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_COMPILED_RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="control",
    declared_tools=("write",),
    oracle_profile=FileToolOracleProfile.TRANSITION_EXACT,
    dependencies=(),
    blocking=False,
    execution_parameters=_BASE_SCENARIO.recipe.execution_parameters,
    bind_oracle_parameters=True,
)
RECIPE = replace(
    _COMPILED_RECIPE,
    setup_commands=_BASE_SCENARIO.recipe.setup_commands,
    seed_files=_BASE_SCENARIO.recipe.seed_files,
    rounds=_BASE_SCENARIO.recipe.rounds,
    oracle_parameters=_BASE_SCENARIO.recipe.oracle_parameters,
)
SCENARIO: FileToolScenario = FileToolScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: FileToolRawObservation):
    """Apply the typed exact-transition contract to the overwrite undo."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str and item.strip().lower() in {"write", "file_write"}
        for item in observation.advertised_tools
    )
    if not advertised and not observation.timed_out:
        return build_file_tool_evaluation(
            SCENARIO,
            observation,
            FileToolJudgement(
                FileToolOutcome.PASS,
                FileToolApplicability.INCONCLUSIVE,
                FileToolOracleProfile.TRANSITION_EXACT.value,
            ),
        )
    facts = dict(observation.facts)
    params = dict(RECIPE.oracle_parameters)
    expected = dict(params["expected"])
    passed = (
        advertised
        and observation.request_count >= int(params["min_requests"])
        and observation.live
        and all(facts.get(key) == value for key, value in expected.items())
        and observation.process_tree_clean
        and observation.gateway_clean
        and observation.workspace_clean
        and not observation.timed_out
    )
    return build_file_tool_evaluation(
        SCENARIO,
        observation,
        FileToolJudgement(
            FileToolOutcome.PASS if passed else FileToolOutcome.FAIL,
            FileToolApplicability.APPLICABLE,
            FileToolOracleProfile.TRANSITION_EXACT.value
            if passed
            else f"{FileToolOracleProfile.TRANSITION_EXACT.value}_FAILED",
        ),
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
