"""Immutable fixture bindings for interactive TUI PTY scenarios."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json

from .contracts import TuiRecipe, TuiScenario, recipe_digest


@dataclass(frozen=True, slots=True)
class TuiFixturePlan:
    case_digest: str
    recipe: TuiRecipe
    recipe_digest: str
    plan_digest: str


def materialize_tui_fixture(scenario: TuiScenario, /) -> TuiFixturePlan:
    body = {
        "case_digest": scenario.recipe.case_digest,
        "recipe_digest": recipe_digest(scenario.recipe),
        "scenario_id": scenario.definition.scenario_id,
    }
    digest = "sha256:" + hashlib.sha256(
        b"doc2harness.tui-pty-fixture/1\0" + canonical_json(body)
    ).hexdigest()
    return TuiFixturePlan(
        scenario.recipe.case_digest,
        scenario.recipe,
        body["recipe_digest"],
        digest,
    )
