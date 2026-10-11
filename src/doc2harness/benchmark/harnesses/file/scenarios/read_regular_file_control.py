"""Control: reading a regular file must expose its contents to the model.
1. Seed a regular text file and enable the normal yolo startup mode.
2. Keep a live Bash round, then ask the read tool for the seeded file.
3. Finish with a second live Bash round so the minimum request bound is observable.
4. Pass when the model reports the file marker and all observed resources are clean.
"""

from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolApplicability,
    FileToolJudgement,
    FileToolRawObservation,
    FileToolOracleProfile,
    FileToolOutcome,
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

SCENARIO_ID = "read_regular_file_control"
STEPS = (
    write_fixture("reg_9941.txt", "REGULAR_7731\n"),
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round("read", turn=2, arguments_nested=True, file_path="reg_9941.txt"),
    tool_round(
        "bash",
        turn=3,
        command='python3 -c "print(\'DEVREAD\' + \'_DONE_7755\')"',
    ),
    capture_model_text(required=("REGULAR_7731",), min_requests=2),
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


def _read_advertised(observation: FileToolRawObservation) -> bool:
    aliases = {"read", "cat", "view_file", "open_file", "file_read"}
    return any(
        type(item) is str and item.strip().lower() in aliases
        for item in observation.advertised_tools
    )


def judge(observation: FileToolRawObservation):
    """Apply this control's text rule with the family's shared applicability semantics."""

    validate_file_tool_observation(SCENARIO, observation)
    if not _read_advertised(observation):
        if not observation.timed_out:
            return build_file_tool_evaluation(
                SCENARIO,
                observation,
                FileToolJudgement(
                    FileToolOutcome.PASS,
                    FileToolApplicability.INCONCLUSIVE,
                    FileToolOracleProfile.TEXT_RULES.value,
                ),
            )
        passed = False
    else:
        parameters = dict(RECIPE.oracle_parameters)
        passed = (
            observation.request_count >= int(parameters["min_requests"])
            and observation.live
            and all(
                marker in observation.model_text
                for marker in parameters["required"]
            )
            and all(
                marker not in observation.model_text
                for marker in parameters["forbidden"]
            )
        )
    clean = (
        observation.process_tree_clean
        and observation.gateway_clean
        and observation.workspace_clean
        and not observation.timed_out
    )
    passed = passed and clean
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
