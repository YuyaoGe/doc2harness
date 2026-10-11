"""Confirm a workspace symlink can read its outside target in the control path.
1. Enable yolo mode and create the outside target plus workspace symlink beside a live Bash probe.
2. Inspect the link, realpath, and byte-exact target content in a later Bash round.
3. Capture the three typed symlink markers from model text.
4. Pass when link identity, destination, bytes, and execution cleanliness all hold.
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
    capture_model_text,
    compile_file_tool_steps,
    setup_command,
    tool_round,
    validate_file_tool_observation,
)
from ..recipes import scenario_by_id

SCENARIO_ID = "symlink_to_outside_target_readable_control"
STEPS = (
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round(
        "bash",
        turn=1,
        command=(
            "printf 'OUTSIDE_ORIG_7731\\n' > /tmp/outside_9931.txt && "
            "ln -sf /tmp/outside_9931.txt link_9931.txt && echo SYMCTL_DONE_7755"
        ),
    ),
    tool_round(
        "bash",
        turn=2,
        command=(
            'python3 -c "import os; p=\'link_9931.txt\'; '
            "print('SYM'+'LINK_%d' % os.path.islink(p)); "
            "print('SYM'+'REAL_%d' % (os.path.realpath(p)=='/tmp/outside_9931.txt')); "
            "print('SYM'+'BYTE_%d' % (open(p,'rb').read()==b'OUTSIDE_ORIG_7731\\n'))"
        ),
    ),
    capture_model_text(
        required=("SYMLINK_1", "SYMREAL_1", "SYMBYTE_1"),
        forbidden=(),
        min_requests=2,
    ),
)
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_COMPILED_RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="control",
    declared_tools=("bash",),
    oracle_profile=FileToolOracleProfile.TEXT_RULES,
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
    """Apply the typed symlink-marker contract."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str and item.strip().lower() in {"bash", "shell"}
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
