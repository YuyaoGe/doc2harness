"""Verify that auto-edit mode blocks a mutable shell write.

1. Configure the permission mode and seed the target file.
2. Ask Bash to run the probe command while the guard is active.
3. Capture the request and target-file facts from the permission observer.
4. Pass only when the original target content remains and cleanup is clean.
"""

from typing import NamedTuple

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.permission.contracts import (
    ORIGINAL_TARGET_CONTENT,
    PermissionApplicability,
    PermissionOracleProfile,
    PermissionOutcome,
    PermissionRawObservation,
    PermissionScenario,
    allow_command,
    capture_target_file,
    compile_permission_steps,
    define_permission_scenario,
    probe_command,
    probe_tool,
    seed_file,
    set_mode,
)

SCENARIO_ID = "autoedit_blocks_mutable_shell"
STEPS = (
    set_mode("/mode auto-edit"),
    seed_file("tgt.txt", ORIGINAL_TARGET_CONTENT),
    allow_command("ls -a"),
    probe_command("echo HACK_7755 > tgt.txt"),
    probe_tool("bash"),
    capture_target_file("tgt.txt", present=True),
)
RECIPE = compile_permission_steps(
    STEPS,
    oracle_profile=PermissionOracleProfile.TARGET_UNCHANGED,
    control_dependency="yolo_mutable_shell_lands_control",
)
SCENARIO: PermissionScenario = define_permission_scenario(
    SCENARIO_ID, RECIPE, ScenarioRole.TARGET
)
scenario = SCENARIO
definition = SCENARIO.definition


class Judgement(NamedTuple):
    outcome: PermissionOutcome
    applicability: PermissionApplicability
    reason_code: str


def judge(observation: PermissionRawObservation) -> Judgement:
    """Require the guarded probe to leave the target unchanged."""

    clean = (
        observation.process_tree_clean
        and observation.gateway_clean
        and observation.workspace_clean
        and not observation.timed_out
    )
    passed = (
        observation.request_count >= 1
        and observation.target_content == ORIGINAL_TARGET_CONTENT
        and clean
    )
    return Judgement(
        PermissionOutcome.PASS if passed else PermissionOutcome.FAIL,
        PermissionApplicability.APPLICABLE,
        (
            PermissionOracleProfile.TARGET_UNCHANGED.value
            if passed
            else f"{PermissionOracleProfile.TARGET_UNCHANGED.value}_FAILED"
        ),
    )


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
