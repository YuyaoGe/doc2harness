"""Execution boundary for startup observations."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

from .contracts import StartupScenario
from .fixtures import StartupFixturePlan, materialize_startup_fixture


@dataclass(frozen=True, slots=True)
class StartupExecutionPlan:
    scenario_id: str
    fixture: StartupFixturePlan
    timeout_seconds: int
    plan_digest: str


def build_startup_execution_plan(scenario: StartupScenario, /) -> StartupExecutionPlan:
    if type(scenario) is not StartupScenario:
        raise ContractDocumentError("startup execution requires an exact scenario")
    fixture = materialize_startup_fixture(scenario)
    body = {
        "fixture_digest": fixture.plan_digest,
        "scenario_id": scenario.definition.scenario_id,
        "timeout_seconds": scenario.recipe.timeout_seconds,
    }
    digest = "sha256:" + hashlib.sha256(
        b"doc2harness.startup-plan/1\0" + canonical_json(body)
    ).hexdigest()
    return StartupExecutionPlan(
        scenario.definition.scenario_id,
        fixture,
        scenario.recipe.timeout_seconds,
        digest,
    )
