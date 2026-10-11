"""Execution boundary for typed headless CLI observations."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

from .contracts import (
    HeadlessProfile,
    HeadlessRecipe,
    HeadlessScenario,
)
from .fixtures import HeadlessFixturePlan, materialize_headless_fixture


@dataclass(frozen=True, slots=True)
class HeadlessExecutionPlan:
    scenario_id: str
    profile: HeadlessProfile
    recipe: HeadlessRecipe
    fixture: HeadlessFixturePlan
    timeout_seconds: int
    plan_digest: str


def build_headless_execution_plan(
    scenario: HeadlessScenario, /
) -> HeadlessExecutionPlan:
    if type(scenario) is not HeadlessScenario:
        raise ContractDocumentError("headless execution requires an exact scenario")
    fixture = materialize_headless_fixture(scenario)
    body = {
        "fixture_digest": fixture.plan_digest,
        "profile": scenario.recipe.profile.value,
        "scenario_id": scenario.definition.scenario_id,
        "timeout_seconds": scenario.recipe.timeout_seconds,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(b"doc2harness.headless-plan/1\0" + canonical_json(body)).hexdigest()
    )
    return HeadlessExecutionPlan(
        scenario.definition.scenario_id,
        scenario.recipe.profile,
        scenario.recipe,
        fixture,
        scenario.recipe.timeout_seconds,
        digest,
    )
