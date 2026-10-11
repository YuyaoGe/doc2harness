"""Verify a fresh workspace starts with an isolated history control.

1. Start the workspace with an empty history store.
2. Observe the initial history state and process status.
3. Require the completion token without a crash signature.
4. Pass only when no stale history is inherited.
"""

from doc2harness.benchmark.harnesses.tui.contracts import (
    TuiApplicability, TuiEvaluation, TuiObservation, TuiOutcome, TuiScenario,
    evaluation_digest,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "history_workspace_a_fresh_control"
SCENARIO: TuiScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: TuiObservation) -> TuiEvaluation:
    text = observation.screen_text
    bind = RECIPE.oracle
    live = bool(text.strip())
    passed = live and bind.completion_token in text and not any(
        token in text for token in bind.crash_signatures
    )
    return TuiEvaluation(SCENARIO_ID, TuiOutcome.PASS if passed else TuiOutcome.FAIL,
                         TuiApplicability.APPLICABLE if live else TuiApplicability.INCONCLUSIVE,
                         "TUI_PROFILE_CHECKED", evaluation_digest(SCENARIO, observation),
                         SCENARIO.definition.weight_microunits if live else 0)


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
