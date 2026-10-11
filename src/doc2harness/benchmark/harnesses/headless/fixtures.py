"""Immutable headless CLI fixture bindings."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json

from .contracts import HeadlessRecipe, HeadlessScenario, recipe_digest


@dataclass(frozen=True, slots=True)
class HeadlessFixturePlan:
    case_digest: str
    recipe: HeadlessRecipe
    recipe_digest: str
    plan_digest: str


def materialize_headless_fixture(scenario: HeadlessScenario, /) -> HeadlessFixturePlan:
    body = {
        "case_digest": scenario.recipe.case_digest,
        "recipe_digest": recipe_digest(scenario.recipe),
        "scenario_id": scenario.definition.scenario_id,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(b"doc2harness.headless-fixture/1\0" + canonical_json(body)).hexdigest()
    )
    return HeadlessFixturePlan(
        scenario.recipe.case_digest,
        scenario.recipe,
        body["recipe_digest"],
        digest,
    )
