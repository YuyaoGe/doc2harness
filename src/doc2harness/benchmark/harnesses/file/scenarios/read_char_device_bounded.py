"""Reject reading a character device while keeping the later round usable.
1. Enable normal yolo mode and start with a live Bash round.
2. Ask the read tool to inspect the bounded character-device path.
3. Send a later Bash round that emits the completion marker.
4. Pass when the completion marker is reported without forbidden output or leaks.
"""

from doc2harness.benchmark.definition import DependencyKind
from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolApplicability,
    FileToolJudgement,
    FileToolRawObservation,
    FileToolOracleProfile,
    FileToolScenario,
    FileToolOutcome,
    build_file_tool_evaluation,
    capture_model_text,
    compile_file_tool_steps,
    setup_command,
    tool_round,
    validate_file_tool_observation,
)
from ..recipes import scenario_by_id

SCENARIO_ID = "read_char_device_bounded"
STEPS = (
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round("read", turn=2, arguments_nested=True, file_path="/dev/urandom"),
    tool_round(
        "bash",
        turn=3,
        command='python3 -c "print(\'DEVREAD\' + \'_DONE_7755\')"',
    ),
    capture_model_text(required=("DEVREAD_DONE_7755",), min_requests=2),
)
RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="reject",
    declared_tools=("read",),
    oracle_profile=FileToolOracleProfile.TEXT_RULES,
    dependencies=(("read_regular_file_control", DependencyKind.GATE),),
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
    """Apply this reject point's completion-marker rule to typed evidence."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str and item.strip().lower() in {"read", "cat", "view_file", "open_file", "file_read"}
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


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
