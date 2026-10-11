"""Confirm that grep finds a known marker in an ordinary workspace file.
1. Seed a text file containing the unique marker and enable yolo mode.
2. Establish a live Bash probe and search the workspace for the marker.
3. Capture the model text that should name the matching file.
4. Pass when the filename is reported and execution remains clean.
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

SCENARIO_ID = "grep_finds_needle_control"
STEPS = (
    write_fixture("plain_gi.txt", "NEEDLE_GI_7731 here\n"),
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round(
        "grep",
        turn=1,
        arguments_nested=True,
        pattern="NEEDLE_GI_7731",
        path=".",
    ),
    capture_model_text(required=("plain_gi.txt",), min_requests=2),
)
RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="control",
    declared_tools=("grep",),
    oracle_profile=FileToolOracleProfile.TEXT_RULES,
    blocking=False,
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
    """Apply the typed grep-result contract without inspecting tool prose."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str
        and item.strip().lower() in {"grep", "search_files", "ripgrep"}
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
