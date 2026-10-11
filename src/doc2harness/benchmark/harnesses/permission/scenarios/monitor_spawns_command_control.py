"""Verify the monitor executed control permission behavior.

1. Configure the mode and prepare the liveness command.
2. Run the monitor probe through the execution adapter.
3. Capture the request and observer-specific facts.
4. Pass only when the expected boundary behavior is observed and cleanup is clean.
"""

from dataclasses import replace
from typing import NamedTuple

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.benchmark.harnesses.permission.contracts import (
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
    set_mode,
)

SCENARIO_ID = "monitor_spawns_command_control"
STEPS = (
    set_mode("/mode yolo"),
    allow_command("echo PERM_LIVE_7755 > live.txt"),
    probe_command("echo MONUNUSED_7755"),
    probe_tool("monitor"),
    capture_target_file("live.txt", present=True),
)
RECIPE = replace(
    compile_permission_steps(
        STEPS, oracle_profile=PermissionOracleProfile.MONITOR_EXECUTED_CONTROL
    ),
    probe_arguments=(
        ("command", "touch monitor_started_7755; sleep 0.2; touch PERM_DENY_7755.txt"),
        ("pattern", "x"),
    ),
    settle_paths=("PERM_DENY_7755.txt", "monitor_started_7755"),
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
    """Apply the profile-specific boundary predicate with clean-run checks."""

    clean = (
        observation.process_tree_clean
        and observation.gateway_clean
        and observation.workspace_clean
        and not observation.timed_out
    )
    passed = (
        observation.deny_file_present
        and "monitor_started_7755" in observation.settled_paths
        and clean
    )
    applicability = PermissionApplicability.APPLICABLE
    return Judgement(
        PermissionOutcome.PASS if passed else PermissionOutcome.FAIL,
        applicability,
        PermissionOracleProfile.MONITOR_EXECUTED_CONTROL.value
        if passed
        else f"{PermissionOracleProfile.MONITOR_EXECUTED_CONTROL.value}_FAILED",
    )


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
