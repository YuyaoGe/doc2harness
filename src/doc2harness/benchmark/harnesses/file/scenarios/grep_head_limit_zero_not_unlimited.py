"""Reject interpreting a zero grep head limit as an unlimited result.
1. Seed a marked file, enable yolo mode, and establish a live Bash round.
2. Search with the nested grep tool using a zero head limit.
3. Run a follow-up completion probe and capture required/forbidden markers.
4. Pass when the completion marker appears without leaking the matching filename.
"""

from doc2harness.benchmark.definition import DependencyKind
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

SCENARIO_ID = "grep_head_limit_zero_not_unlimited"
STEPS = (
    write_fixture("f9_9931.txt", "F9NEEDLE_7755 ctxf9_9931\n"),
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round(
        "grep",
        turn=1,
        arguments_nested=True,
        pattern="F9NEEDLE_7755",
        path=".",
        head_limit=0,
    ),
    tool_round("bash", turn=2, command="echo HZ9_DONE_7755"),
    capture_model_text(
        required=("HZ9_DONE_7755",),
        forbidden=("f9_9931",),
        min_requests=2,
    ),
)
RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="reject",
    declared_tools=("grep",),
    oracle_profile=FileToolOracleProfile.TEXT_RULES,
    dependencies=(("grep_default_mode_returns_path_control", DependencyKind.GATE),),
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
    """Apply typed zero-limit output rules without parsing tool prose."""

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
