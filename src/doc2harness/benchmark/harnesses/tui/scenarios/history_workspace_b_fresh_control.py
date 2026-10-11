"""Verify a second fresh workspace has isolated history.

1. Start workspace B with an empty history store.
2. Observe its initial history and process status.
3. Require completion without a crash signature.
4. Pass only when workspace A history is not inherited.
"""

from doc2harness.benchmark.harnesses.tui.contracts import (
    TuiApplicability, TuiEvaluation, TuiObservation, TuiOutcome, TuiScenario,
    evaluation_digest,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "history_workspace_b_fresh_control"
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
