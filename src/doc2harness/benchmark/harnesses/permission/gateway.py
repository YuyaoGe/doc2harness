"""Deterministic gateway plan for child-process permission probes."""

from __future__ import annotations

from dataclasses import dataclass

from doc2harness.benchmark.harnesses.permission.contracts import (
    PermissionScenario,
)
from doc2harness.contracts.primitives import ContractDocumentError


@dataclass(frozen=True, slots=True)
class PermissionToolCall:
    call_id: str
    tool_name: str
    arguments: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PermissionGatewayTurn:
    tool_calls: tuple[PermissionToolCall, ...]
    assistant_text: str


def build_permission_gateway_plan(
    scenario: PermissionScenario, /
) -> tuple[PermissionGatewayTurn, ...]:
    """Return the two-turn mock dialogue without owning sockets or processes."""

    if type(scenario) is not PermissionScenario:
        raise ContractDocumentError("permission gateway requires an exact scenario")
    recipe = scenario.recipe
    allow = PermissionToolCall(
        "permission-allow", "bash", (("command", recipe.allow_command),)
    )
    probe_args = recipe.probe_arguments or (("command", recipe.probe_command),)
    probe = PermissionToolCall("permission-probe", recipe.probe_tool, probe_args)
    calls = (allow, probe)
    if recipe.second_probe is not None:
        calls += (
            PermissionToolCall(
                "permission-probe-secondary",
                "bash",
                (("command", recipe.second_probe),),
            ),
        )
    return (
        PermissionGatewayTurn(calls, ""),
        PermissionGatewayTurn((), "done"),
    )


__all__ = [
    "PermissionGatewayTurn",
    "PermissionToolCall",
    "build_permission_gateway_plan",
]
