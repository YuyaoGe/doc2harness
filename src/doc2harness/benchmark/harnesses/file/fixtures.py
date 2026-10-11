"""Deterministic fixture materialization for file-tool child scenarios."""

import hashlib
from dataclasses import dataclass

from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolScenario,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError


@dataclass(frozen=True, slots=True)
class FileToolFixturePlan:
    scenario_id: str
    seed_files: tuple[tuple[str, str], ...]
    plan_digest: str


def materialize_file_tool_fixture(scenario: FileToolScenario, /) -> FileToolFixturePlan:
    if type(scenario) is not FileToolScenario:
        raise ContractDocumentError("file-tool fixture requires an exact scenario")
    body = {
        "scenario_id": scenario.definition.scenario_id,
        "seed_files": [list(x) for x in scenario.recipe.seed_files],
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            b"doc2harness.file-tool-child-fixture/1\0" + canonical_json(body)
        ).hexdigest()
    )
    return FileToolFixturePlan(
        scenario.definition.scenario_id, scenario.recipe.seed_files, digest
    )


__all__ = ["FileToolFixturePlan", "materialize_file_tool_fixture"]
