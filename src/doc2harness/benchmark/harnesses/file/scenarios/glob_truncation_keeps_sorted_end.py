"""Verify a truncated glob result keeps the sorted tail instead of only old entries.
1. Enable yolo mode and seed one old file plus 800 numbered ``.ordx`` files through Bash.
2. Search the workspace with the nested recursive glob pattern.
3. Capture model text containing the suffix and excluding the oldest-file marker.
4. Pass when the sorted result reaches the expected tail and execution is clean.
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
)
from ..recipes import scenario_by_id

SCENARIO_ID = "glob_truncation_keeps_sorted_end"
STEPS = (
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round(
        "bash",
        turn=1,
        command=(
            "mkdir -p gord && python3 -c \"import pathlib;pathlib.Path('gord/OLD'"
            " + 'EST_7755.ordx').write_text('x')\" && touch -t 200001010000 "
            "gord/OLD*_7755.ordx && python3 -c \"import os\n"
            "for i in range(800): open('gord/n%d.ordx' % i,'w').write('x')\""
            " && echo ORD_SEEDED_7755"
        ),
    ),
    tool_round("glob", turn=2, arguments_nested=True, pattern="**/*.ordx"),
    capture_model_text(
        required=(".ordx",),
        forbidden=("OLDEST_7755",),
        min_requests=2,
    ),
)
RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="reject",
    declared_tools=("glob",),
    oracle_profile=FileToolOracleProfile.TEXT_RULES,
    dependencies=(("glob_finds_inside_control", DependencyKind.GATE),),
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
    """Apply typed glob-tail rules without parsing tool prose."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str and item.strip().lower() in {"glob", "find_files"}
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
    source = getattr(observation, str(params["source"]))
    passed = (
        advertised
        and observation.request_count >= int(params["min_requests"])
        and observation.live
        and all(marker in source for marker in params["required"])
        and all(marker not in source for marker in params["forbidden"])
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
