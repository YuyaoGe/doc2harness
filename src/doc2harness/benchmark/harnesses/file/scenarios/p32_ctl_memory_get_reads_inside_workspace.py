"""Confirm memory-get observes a workspace memory file after phase-scoped setup.
1. Seed `MEMORY.md`, enable yolo mode, and run the phase-1 live Bash control round.
2. In phase 2, request the memory file through both memory-get aliases.
3. Emit a completion marker and capture separate phase-1/phase-2 model facts.
4. Pass when the marker count increases in phase 2 and all execution surfaces remain clean.
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

SCENARIO_ID = "p32_ctl_memory_get_reads_inside_workspace"
STEPS = (
    write_fixture("MEMORY.md", "INSIDEMEM_7732\n"),
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round(
        ("memory_get", "memory-get"),
        turn=2,
        aliases_explicit=True,
        arguments_nested=True,
        path="MEMORY.md",
    ),
    tool_round("bash", turn=3, command="echo P32A_DONE_7755"),
    capture_fact("phase2_model_text", required=("INSIDEMEM_7732",), min_requests=2),
)
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_COMPILED_RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="control",
    declared_tools=("memory_get",),
    oracle_profile=FileToolOracleProfile.MEMORY_EVENT,
    blocking=True,
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
    """Apply the typed phase-scoped memory-event and completion contract."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str
        and item.strip().lower().replace("-", "_") in {"memory_get", "memoryget"}
        for item in observation.advertised_tools
    )
    if not advertised and not observation.timed_out:
        return build_file_tool_evaluation(
            SCENARIO,
            observation,
            FileToolJudgement(
                FileToolOutcome.PASS,
                FileToolApplicability.INCONCLUSIVE,
                FileToolOracleProfile.MEMORY_EVENT.value,
            ),
        )
    params = dict(RECIPE.oracle_parameters)
    facts = dict(observation.facts)
    before = facts.get("phase1_model_text")
    after = facts.get("phase2_model_text")
    marker = str(params["marker"])
    passed = (
        advertised
        and observation.request_count >= int(params["min_requests"])
        and observation.live
        and type(before) is str
        and type(after) is str
        and after.count(marker) > before.count(marker)
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
            FileToolOracleProfile.MEMORY_EVENT.value
            if passed
            else f"{FileToolOracleProfile.MEMORY_EVENT.value}_FAILED",
        ),
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
