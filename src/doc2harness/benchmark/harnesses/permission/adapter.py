"""Execution adapter for the permission-child Harness layer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from doc2harness.benchmark.harnesses.permission.contracts import (
    PermissionScenario,
)
from doc2harness.benchmark.harnesses.permission.fixtures import (
    PermissionFixturePlan,
    materialize_permission_fixture,
)
from doc2harness.benchmark.harnesses.permission.gateway import (
    PermissionGatewayTurn,
    build_permission_gateway_plan,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError


@dataclass(frozen=True, slots=True)
class PermissionExecutionPlan:
    scenario_id: str
    fixture: PermissionFixturePlan
    gateway_turns: tuple[PermissionGatewayTurn, ...]
    deny_rules: tuple[str, ...]
    setup_commands: tuple[str, ...]
    variant_command: str | None
    counterfactual_setup_commands: tuple[str, ...]
    timeout_seconds: int
    plan_digest: str


def build_permission_execution_plan(
    scenario: PermissionScenario, /
) -> PermissionExecutionPlan:
    if type(scenario) is not PermissionScenario:
        raise ContractDocumentError("permission execution requires an exact scenario")
    fixture = materialize_permission_fixture(scenario)
    turns = build_permission_gateway_plan(scenario)
    body = {
        "deny_rules": list(scenario.recipe.deny_rules),
        "fixture_digest": fixture.plan_digest,
        "gateway": [
            {
                "assistant_text": turn.assistant_text,
                "tool_calls": [
                    {
                        "arguments": list(call.arguments),
                        "call_id": call.call_id,
                        "tool_name": call.tool_name,
                    }
                    for call in turn.tool_calls
                ],
            }
            for turn in turns
        ],
        "scenario_id": scenario.definition.scenario_id,
        "setup_commands": list(scenario.recipe.setup_commands),
        "variant_command": scenario.recipe.variant_command,
        "counterfactual_setup_commands": list(
            scenario.recipe.counterfactual_setup_commands
        ),
        "timeout_seconds": scenario.recipe.timeout_seconds,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            b"doc2harness.permission-child-plan/1\0" + canonical_json(body)
        ).hexdigest()
    )
    return PermissionExecutionPlan(
        scenario.definition.scenario_id,
        fixture,
        turns,
        scenario.recipe.deny_rules,
        scenario.recipe.setup_commands,
        scenario.recipe.variant_command,
        scenario.recipe.counterfactual_setup_commands,
        scenario.recipe.timeout_seconds,
        digest,
    )


__all__ = [
    "PermissionExecutionPlan",
    "build_permission_execution_plan",
]
