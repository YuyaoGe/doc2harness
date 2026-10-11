"""Verify rewind restores an overwritten file from an earlier snapshot.
1. Seed the target, enable yolo mode, read it beside a live Bash probe, and overwrite it.
2. Rewind through the available history boundary.
3. Run a post-rewind probe and capture the phase snapshots.
4. Pass when the rewind restores the expected original content and execution is clean.
"""

from dataclasses import replace

from doc2harness.benchmark.definition import DependencyKind
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

SCENARIO_ID = "rewind_restores_edited_file"
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
        content="RWOVR_8821\n",
    ),
    tool_round("bash", turn=3, command="echo POSTRW_8821"),
    capture_fact("target_content", required=(), min_requests=2),
)
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_COMPILED_RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="reject",
    declared_tools=("write",),
    oracle_profile=FileToolOracleProfile.REWIND_RESTORES,
    dependencies=(("undo_reverts_overwrite_control", DependencyKind.GATE),),
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
    """Apply the typed rewind-restores contract to phase snapshots."""

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
                FileToolOracleProfile.REWIND_RESTORES.value,
            ),
        )
    facts = dict(observation.facts)
    params = dict(RECIPE.oracle_parameters)
    expected = str(params["expected"])
    before = facts.get("phase1_tgt")
    unknown = facts.get("rewind_unknown") is True
    if (unknown or before == expected) and not observation.timed_out:
        return build_file_tool_evaluation(
            SCENARIO,
            observation,
            FileToolJudgement(
                FileToolOutcome.PASS,
                FileToolApplicability.INCONCLUSIVE,
                FileToolOracleProfile.REWIND_RESTORES.value,
            ),
        )
    passed = (
        advertised
        and observation.request_count >= int(params["min_requests"])
        and observation.live
        and facts.get("after_mid1_tgt") == expected
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
            FileToolOracleProfile.REWIND_RESTORES.value
            if passed
            else f"{FileToolOracleProfile.REWIND_RESTORES.value}_FAILED",
        ),
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
