"""Verify a standalone bang enters shell mode and returns cleanly.

1. Submit the standalone bang command and observe shell-mode output.
2. Submit the follow-up probe turn.
3. Require completion without a crash signature.
4. Pass only when shell-mode interaction remains usable.
"""

from doc2harness.benchmark.harnesses.tui.contracts import (
    TuiApplicability, TuiEvaluation, TuiObservation, TuiOutcome, TuiScenario,
    evaluation_digest,
)

from ..recipes import scenario_by_id

SCENARIO_ID = "tui_bang_alone_enters_shell_mode"
SCENARIO: TuiScenario = scenario_by_id(SCENARIO_ID)
RECIPE = SCENARIO.recipe
STEPS = (RECIPE,)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: TuiObservation) -> TuiEvaluation:
    text = observation.screen_text
    bind = RECIPE.oracle
    live = bool(text.strip())
    # The declared oracle for this point is FILE_WRITTEN: the shell line must
    # really run and leave the file behind.  Judging the survival sentinel
    # instead asks whether the *driver's own* closing line was echoed, which a
    # session answers whether or not the point's line did anything.  Read the
    # file the recipe
    # declared, exactly as the shared oracle for this profile does.
    item = next(
        (entry for entry in observation.files if entry.path == bind.path), None,
    )
    passed = (
        live
        and item is not None
        and item.present
        and bind.primary_token in item.content
        and not any(token in text for token in bind.crash_signatures)
    )
    return TuiEvaluation(SCENARIO_ID, TuiOutcome.PASS if passed else TuiOutcome.FAIL,
                         TuiApplicability.APPLICABLE if live else TuiApplicability.INCONCLUSIVE,
                         "TUI_PROFILE_CHECKED", evaluation_digest(SCENARIO, observation),
                         SCENARIO.definition.weight_microunits if live else 0)


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
