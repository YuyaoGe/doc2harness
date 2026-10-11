"""Confirm default grep output reports matching paths without line content.
1. Seed a file containing a unique marker and enable yolo mode.
2. Run a live Bash probe and grep with the path-only output mode.
3. Capture the typed grep result and a completion marker from the follow-up round.
4. Pass when the filename is present, content and line-number output are absent, and execution is clean.
"""

import re
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

SCENARIO_ID = "grep_default_mode_returns_path_control"
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
        output_mode="files_with_matches",
    ),
    tool_round("bash", turn=2, command="echo F9C_DONE_7755"),
    capture_fact(
        "target_tool_result",
        required=("f9_9931.txt",),
        forbidden=("ctxf9_9931",),
        forbidden_regex=r"(?:^|\n)\s*[^:\n]+:\s*\d+\s*(?:\n|$)",
        min_requests=2,
    ),
)
RECIPE = replace(
    compile_file_tool_steps(
        STEPS,
        case_kind="control",
        declared_tools=("grep",),
        oracle_profile=FileToolOracleProfile.TEXT_RULES,
        blocking=True,
        execution_parameters=(
            ("deny", ()),
            ("allow", "echo PERM_LIVE_7755 > live.txt"),
        ),
        bind_oracle_parameters=True,
    ),
    # The parameter order is part of the frozen recipe identity.
    oracle_parameters=(
        ("source", "target_tool_result"),
        ("required", ("f9_9931.txt",)),
        ("forbidden", ("ctxf9_9931",)),
        (
            "forbidden_regex",
            r"(?:^|\n)\s*[^:\n]+:\s*\d+\s*(?:\n|$)",
        ),
        ("min_requests", 2),
    ),
)
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
SCENARIO: FileToolScenario = FileToolScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: FileToolRawObservation):
    """Apply typed path-only grep rules without parsing tool prose."""

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
    source = getattr(observation, str(params["source"]))
    passed = (
        advertised
        and observation.request_count >= int(params["min_requests"])
        and observation.live
        and all(marker in source for marker in params["required"])
        and all(marker not in source for marker in params["forbidden"])
        and (
            params.get("forbidden_regex") is None
            or re.search(str(params["forbidden_regex"]), source, re.MULTILINE) is None
        )
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
