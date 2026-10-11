"""Execution boundary for typed TUI PTY observations."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole

from .contracts import (
    TuiApplicability,
    TuiEvaluation,
    TuiInput,
    TuiObservation,
    TuiOracleBinding,
    TuiOutcome,
    TuiProfile,
    TuiRecipe,
    TuiScenario,
    define_tui_scenario,
    evaluation_digest,
)
from .fixtures import TuiFixturePlan, materialize_tui_fixture


@dataclass(frozen=True, slots=True)
class TuiPtyExecutionPlan:
    scenario_id: str
    profile: TuiProfile
    recipe: TuiRecipe
    fixture: TuiFixturePlan
    timeout_seconds: int
    plan_digest: str


def build_tui_pty_execution_plan(scenario: TuiScenario, /) -> TuiPtyExecutionPlan:
    if type(scenario) is not TuiScenario:
        raise ContractDocumentError("TUI PTY execution requires an exact scenario")
    fixture = materialize_tui_fixture(scenario)
    body = {
        "fixture_digest": fixture.plan_digest,
        "profile": scenario.recipe.profile.value,
        "scenario_id": scenario.definition.scenario_id,
        "timeout_seconds": scenario.recipe.timeout_seconds,
    }
    digest = "sha256:" + hashlib.sha256(
        b"doc2harness.tui-pty-plan/1\0" + canonical_json(body)
    ).hexdigest()
    return TuiPtyExecutionPlan(
        scenario.definition.scenario_id,
        scenario.recipe.profile,
        scenario.recipe,
        fixture,
        scenario.recipe.timeout_seconds,
        digest,
    )


# These builders intentionally live at the adapter boundary rather
# than in a second helper module: scenario files stay declarative while the
# runtime-owned external observation policy remains in one stable module.
def build_external_tui_scenario(
    scenario_id: str,
    role: ScenarioRole,
    control_id: str | None,
    gateway_mode: str,
) -> TuiScenario:
    dependencies = () if role is ScenarioRole.CONTROL else (
        (control_id, DependencyKind.GATE),
    )
    digest = "sha256:" + hashlib.sha256(
        ("doc2harness.tui.external/1\\0" + scenario_id).encode()
    ).hexdigest()
    marker = "TUI_EXTERNAL_7731"
    cli_arguments: tuple[str, ...] = ()
    if "background_completion" in scenario_id:
        marker = "TUI_BACKGROUND_DONE_7731"
    elif "autotest_output" in scenario_id:
        marker = "TUI_AUTOTEST_SUCCESS_7731"
        cli_arguments = (
            "--auto-test",
            "--test-cmd",
            "printf 'TUI_AUTOTEST_SUCCESS_7731 stdout test passed\\n'",
        )
    elif "autocommit_fail" in scenario_id:
        marker = "TUI_AUTOCOMMIT_FAIL_7731"
        cli_arguments = ("--auto-commit",)
    elif "bad_tool_args" in scenario_id:
        marker = "TUI_BAD_ARGS_SUCCESS_7731"
    elif "empty_tool_name" in scenario_id:
        marker = "TUI_EMPTY_TOOL_7731"
    elif "model_finish_reason" in scenario_id:
        marker = "TUI_FINISH_REASON_7731"
    elif "step_limit" in scenario_id:
        marker = "TUI_STEP_LIMIT_7731"
        cli_arguments = ("--max-steps", "2")
    recipe = TuiRecipe(
        TuiProfile.WIRE_SURVIVES,
        digest,
        dependencies,
        180,
        False,
        input_lines=(TuiInput("70696e67"),),
        gateway_mode=gateway_mode,
        probe_tokens=(marker,),
        cli_arguments=cli_arguments,
        oracle=TuiOracleBinding(primary_token=marker),
    )
    return define_tui_scenario(scenario_id, recipe, role)


def _external_tui_has(text: str, *patterns: str) -> bool:
    lowered = text.lower()
    return all(
        any(pattern.lower() in lowered for pattern in group.split("|"))
        for group in patterns
    )


def _external_tui_requirement(requirement: str, text: str, marker: str = "") -> bool:
    """Check a trajectory requirement using its scenario-bound canary marker.

    The recipe tables use generic marker names, while the external recipe
    builder uses a stable per-scenario marker.  Accepting the bound marker as
    an alternative keeps the judge tied to the scenario rather than to one
    probe spelling; it does not make a phrase-only trajectory
    sufficient because every branch still requires the semantic event words.
    """

    marker = marker or "__missing_marker__"
    if requirement == "step_limit":
        return _external_tui_has(text, "limits_exceeded|max_steps", "run_end|terminal|finished")
    if requirement == "bad_args":
        return _external_tui_has(text, "tool_result|error|failure", "invalid|malformed|json|argument", f"TUI_BAD_ARGS_SUCCESS_7731|{marker}")
    if requirement == "autotest":
        return _external_tui_has(text, "stdout|test", "success|passed|ok", f"TUI_AUTOTEST_SUCCESS_7731|{marker}")
    if requirement == "autocommit_fail":
        return _external_tui_has(text, "auto.?commit|commit", "gpg|hook|cannot|failed|error", f"TUI_AUTOCOMMIT_FAIL_7731|{marker}")
    if requirement == "background":
        return _external_tui_has(text, "completed|exit|finished", f"TUI_BACKGROUND_DONE_7731|{marker}")
    if requirement == "empty_reply":
        return _external_tui_has(text, "warning|error|note", "empty|no content|blank")
    if requirement == "finish_reason":
        return _external_tui_has(text, "tool_calls|tool call", "finish_reason|stop")
    if requirement == "empty_tool_name":
        return _external_tui_has(
            text,
            "error|invalid|malformed|no tool named|unknown tool",
            "empty|missing|invalid|no tool named|unknown tool|''",
            "tool",
        )
    raise ValueError(requirement)


def make_external_tui_judge(scenario: TuiScenario, requirement: str):
    def judge(observation: TuiObservation) -> TuiEvaluation:
        if observation.scenario_id != scenario.definition.scenario_id:
            raise ValueError("TUI external observation belongs to another scenario")
        base_ready = (
            observation.cli_available
            and bool(observation.requests)
            and observation.returncode == 0
            and not observation.driver_error
            and observation.process_tree_clean
            and observation.workspace_clean
            and not observation.deadline_reached
        )
        if not base_ready:
            outcome = TuiOutcome.PASS
            applicability = TuiApplicability.INCONCLUSIVE
            reason = "TUI_EXTERNAL_STIMULUS_UNESTABLISHED"
        elif scenario.definition.role is ScenarioRole.CONTROL:
            outcome = TuiOutcome.PASS
            applicability = TuiApplicability.APPLICABLE
            reason = "TUI_EXTERNAL_CONTROL_ESTABLISHED"
        elif not observation.trajectory_text:
            outcome = TuiOutcome.FAIL
            applicability = TuiApplicability.APPLICABLE
            reason = "TUI_TRAJECTORY_NOT_OBSERVED"
        else:
            marker = scenario.recipe.oracle.primary_token or (
                scenario.recipe.probe_tokens[0] if scenario.recipe.probe_tokens else ""
            )
            passed = _external_tui_requirement(requirement, observation.trajectory_text, marker)
            outcome = TuiOutcome.PASS if passed else TuiOutcome.FAIL
            applicability = TuiApplicability.APPLICABLE
            reason = "TUI_TRAJECTORY_REQUIREMENT_RECORDED" if passed else "TUI_TRAJECTORY_REQUIREMENT_MISSING"
        contribution = (
            scenario.definition.weight_microunits
            if outcome is TuiOutcome.PASS and applicability is TuiApplicability.APPLICABLE
            else 0
        )
        return TuiEvaluation(
            scenario.definition.scenario_id,
            outcome,
            applicability,
            reason,
            evaluation_digest(scenario, observation),
            contribution,
        )

    return judge


# Short aliases keep the scenario declarations tiny.
build_scenario = build_external_tui_scenario
make_judge = make_external_tui_judge


__all__ = [
    "TuiPtyExecutionPlan", "build_tui_pty_execution_plan",
    "build_external_tui_scenario", "make_external_tui_judge",
    "build_scenario", "make_judge",
]
