"""Control that proves yolo mode allows both write and read operations.

1. Enter yolo mode, seed readable and protected fixtures, and prepare liveness.
2. Run the write probe followed by its read follow-up through Bash.
3. Capture request, target, and output facts from the permission observer.
4. Pass only when the target changes, the read token is visible, and cleanup is clean.
"""

from dataclasses import replace
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

SCENARIO_ID = "p60_ctl_yolo_both_work"
EXPECTED_TOKEN = "ASKMODE_BOTH_9942"
STEPS = (
    set_mode("/mode yolo"),
    seed_file("rdt.txt", "ASKMODE_BOTH_9942 readable\n"),
    seed_file("tgt.txt", ORIGINAL_TARGET_CONTENT),
    allow_command("echo PERM_LIVE_7755 > live.txt"),
    probe_command("echo HACK_7755 > tgt.txt"),
    probe_tool("bash"),
    capture_target_file("tgt.txt", present=True),
)
RECIPE = replace(
    compile_permission_steps(
        STEPS, oracle_profile=PermissionOracleProfile.WRITE_AND_READ_EXECUTED
    ),
    second_probe="cat rdt.txt",
    expected_token=EXPECTED_TOKEN,
)
SCENARIO: PermissionScenario = define_permission_scenario(
    SCENARIO_ID, RECIPE, ScenarioRole.CONTROL
)
scenario = SCENARIO
definition = SCENARIO.definition


class Judgement(NamedTuple):
    outcome: PermissionOutcome
    applicability: PermissionApplicability
    reason_code: str


def judge(observation: PermissionRawObservation) -> Judgement:
    """Require a mutated target and visible read token with clean boundaries."""

    clean = (
        observation.process_tree_clean
        and observation.gateway_clean
        and observation.workspace_clean
        and not observation.timed_out
    )
    expected_token = EXPECTED_TOKEN or "RDTK_4242X"
    observed_text = observation.messages_text + "\n" + observation.stdout_text
    passed = (
        observation.request_count >= 1
        and observation.target_content != ORIGINAL_TARGET_CONTENT
        and expected_token in observed_text
        and clean
    )
    return Judgement(
        PermissionOutcome.PASS if passed else PermissionOutcome.FAIL,
        PermissionApplicability.APPLICABLE,
        PermissionOracleProfile.WRITE_AND_READ_EXECUTED.value
        if passed
        else f"{PermissionOracleProfile.WRITE_AND_READ_EXECUTED.value}_FAILED",
    )


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
