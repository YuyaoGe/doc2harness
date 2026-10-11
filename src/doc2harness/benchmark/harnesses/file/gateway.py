"""Deterministic mock-gateway plan for file-tool child probes."""

from dataclasses import dataclass

from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolCall,
    FileToolScenario,
)
from doc2harness.contracts.primitives import ContractDocumentError


@dataclass(frozen=True, slots=True)
class FileToolGatewayTurn:
    tool_calls: tuple[FileToolCall, ...]


def build_file_tool_gateway_plan(
    scenario: FileToolScenario, /
) -> tuple[FileToolGatewayTurn, ...]:
    if type(scenario) is not FileToolScenario:
        raise ContractDocumentError("file-tool gateway requires an exact scenario")
    return tuple(FileToolGatewayTurn(turn) for turn in scenario.recipe.rounds)


__all__ = ["FileToolGatewayTurn", "build_file_tool_gateway_plan"]
