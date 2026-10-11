"""Execution boundary for the file-tool child Harness layer."""

import hashlib
from dataclasses import dataclass

from doc2harness.benchmark.harnesses.file.contracts import FileToolScenario, FrozenValue
from doc2harness.benchmark.harnesses.file.fixtures import (
    FileToolFixturePlan,
    materialize_file_tool_fixture,
)
from doc2harness.benchmark.harnesses.file.gateway import (
    FileToolGatewayTurn,
    build_file_tool_gateway_plan,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError


@dataclass(frozen=True, slots=True)
class FileToolExecutionPlan:
    scenario_id: str
    fixture: FileToolFixturePlan
    gateway_turns: tuple[FileToolGatewayTurn, ...]
    setup_commands: tuple[str, ...]
    timeout_seconds: int
    plan_digest: str
    execution_parameters: tuple[tuple[str, FrozenValue], ...] = ()


def build_file_tool_execution_plan(
    scenario: FileToolScenario, /
) -> FileToolExecutionPlan:
    if type(scenario) is not FileToolScenario:
        raise ContractDocumentError("file-tool execution requires an exact scenario")
    fixture = materialize_file_tool_fixture(scenario)
    turns = build_file_tool_gateway_plan(scenario)
    body = {
        "fixture_digest": fixture.plan_digest,
        "gateway": [
            [
                {
                    "aliases_explicit": call.aliases_explicit,
                    "arguments": list(call.arguments),
                    "arguments_nested": call.arguments_nested,
                    "tool_names": list(call.tool_names),
                }
                for call in turn.tool_calls
            ]
            for turn in turns
        ],
        "scenario_id": scenario.definition.scenario_id,
        "setup_commands": list(scenario.recipe.setup_commands),
        "timeout_seconds": scenario.recipe.timeout_seconds,
    }
    if scenario.recipe.execution_parameters:
        body["execution_parameters"] = list(scenario.recipe.execution_parameters)
    digest = (
        "sha256:"
        + hashlib.sha256(
            b"doc2harness.file-tool-child-plan/1\0" + canonical_json(body)
        ).hexdigest()
    )
    return FileToolExecutionPlan(
        scenario.definition.scenario_id,
        fixture,
        turns,
        scenario.recipe.setup_commands,
        scenario.recipe.timeout_seconds,
        digest,
        scenario.recipe.execution_parameters,
    )


__all__ = [
    "FileToolExecutionPlan",
    "build_file_tool_execution_plan",
]
