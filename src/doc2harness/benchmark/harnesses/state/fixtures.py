"""Deterministic fixture plans for child-process state scenarios."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

from .contracts import FrozenValue, StateScenario


@dataclass(frozen=True, slots=True)
class StateFixturePlan:
    scenario_id: str
    seed_files: tuple[tuple[str, str], ...]
    execution_parameters: tuple[tuple[str, FrozenValue], ...]
    logical_bindings: tuple[tuple[str, str], ...]
    plan_digest: str
    home_seed_files: tuple[tuple[str, str], ...] = ()


def materialize_state_fixture(scenario: StateScenario, /) -> StateFixturePlan:
    if type(scenario) is not StateScenario:
        raise ContractDocumentError("state fixture requires an exact scenario")
    recipe = scenario.recipe
    body = {
        "execution_parameters": recipe.execution_parameters,
        "home_seed_files": recipe.home_seed_files,
        "logical_bindings": (
            ("@python", "runtime-python"),
            ("@state-mcp-fixture", "state-mcp-peer"),
        ),
        "scenario_id": scenario.definition.scenario_id,
        "seed_files": recipe.seed_files,
    }
    digest = "sha256:" + hashlib.sha256(
        b"doc2harness.state-child-fixture/1\0" + canonical_json(body)
    ).hexdigest()
    return StateFixturePlan(
        scenario.definition.scenario_id,
        recipe.seed_files,
        recipe.execution_parameters,
        (("@python", "runtime-python"), ("@state-mcp-fixture", "state-mcp-peer")),
        digest,
        recipe.home_seed_files,
    )


__all__ = ["StateFixturePlan", "materialize_state_fixture"]
