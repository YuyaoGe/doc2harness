"""Confirm that a workspace file symlink remains readable through the read tool.
1. Seed an in-workspace file, enable yolo mode, and create a symlink to it.
2. Read the symlink with the nested read arguments.
3. Send a later Bash completion marker after the read result is delivered.
4. Pass when the file marker and completion are visible with clean execution.
"""

from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolApplicability,
    FileToolJudgement,
    FileToolOracleProfile,
    FileToolOutcome,
    FileToolRawObservation,
    FileToolScenario,
    build_file_tool_evaluation,
    capture_model_text,
    compile_file_tool_steps,
    setup_command,
    tool_round,
    validate_file_tool_observation,
    write_fixture,
)
from ..recipes import scenario_by_id

SCENARIO_ID = "file_symlink_inside_still_readable_control"
STEPS = (
    write_fixture("rif_9931.txt", "READ_INSIDE_7731\n"),
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round(
        "bash",
        turn=1,
        command="ln -sf rif_9931.txt ril_9931.txt && echo RIF_SEEDED_7755",
    ),
    tool_round("read", turn=2, arguments_nested=True, file_path="ril_9931.txt"),
    tool_round("bash", turn=3, command="echo RIF_DONE_7755"),
    capture_model_text(required=("READ_INSIDE_7731",), min_requests=2),
)
RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="control",
    declared_tools=("read",),
    oracle_profile=FileToolOracleProfile.TEXT_RULES,
    blocking=True,
    execution_parameters=(
        ("deny", ()),
        ("allow", "echo PERM_LIVE_7755 > live.txt"),
    ),
    bind_oracle_parameters=True,
)
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
SCENARIO: FileToolScenario = FileToolScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: FileToolRawObservation):
    """Apply the typed readable-symlink and completion contract."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str
        and item.strip().lower() in {"read", "cat", "view_file", "open_file", "file_read"}
        for item in observation.advertised_tools
    )
    if not advertised and not observation.timed_out:
        return build_file_tool_evaluation(
            SCENARIO,
            observation,
            FileToolJudgement(
                FileToolOutcome.PASS,
                FileToolApplicability.INCONCLUSIVE,
                FileToolOracleProfile.TEXT_RULES.value,
            ),
        )
    params = dict(RECIPE.oracle_parameters)
    passed = (
        advertised
        and observation.request_count >= int(params["min_requests"])
        and observation.live
        and all(marker in observation.model_text for marker in params["required"])
        and all(marker not in observation.model_text for marker in params["forbidden"])
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
            FileToolOracleProfile.TEXT_RULES.value
            if passed
            else f"{FileToolOracleProfile.TEXT_RULES.value}_FAILED",
        ),
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
