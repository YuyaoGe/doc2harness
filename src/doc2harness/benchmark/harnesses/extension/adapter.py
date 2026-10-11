"""Execution boundary for extension discovery counterfactuals."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

from .contracts import (
    ExtensionCounterfactualScenario,
)
from .fixtures import ExtensionFixturePlan, materialize_extension_fixture


@dataclass(frozen=True, slots=True)
class ExtensionExecutionPlan:
    scenario_id: str
    fixture: ExtensionFixturePlan
    setup_commands: tuple[str, ...]
    round_tool: str
    round_command: str
    paired: bool
    timeout_seconds: int
    plan_digest: str


def build_extension_execution_plan(
    scenario: ExtensionCounterfactualScenario,
    /,
) -> ExtensionExecutionPlan:
    if type(scenario) is not ExtensionCounterfactualScenario:
        raise ContractDocumentError("extension execution requires an exact scenario")
    recipe = scenario.recipe
    fixture = materialize_extension_fixture(scenario)
    body = {
        "fixture_digest": fixture.plan_digest,
        "paired": recipe.paired,
        "round": {"command": recipe.round_command, "tool": recipe.round_tool},
        "scenario_id": scenario.definition.scenario_id,
        "setup_commands": list(recipe.setup_commands),
        "timeout_seconds": recipe.timeout_seconds,
    }
    digest = "sha256:" + hashlib.sha256(
        b"doc2harness.extension-counterfactual-plan/1\0" + canonical_json(body)
    ).hexdigest()
    return ExtensionExecutionPlan(
        scenario.definition.scenario_id,
        fixture,
        recipe.setup_commands,
        recipe.round_tool,
        recipe.round_command,
        recipe.paired,
        recipe.timeout_seconds,
        digest,
    )


__all__ = [
    "ExtensionExecutionPlan",
    "build_extension_execution_plan",
]
