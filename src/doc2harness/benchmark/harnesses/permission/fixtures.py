"""Deterministic fixture materialization for permission-child scenarios."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from doc2harness.benchmark.harnesses.permission.contracts import (
    PermissionScenario,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError


@dataclass(frozen=True, slots=True)
class PermissionFixturePlan:
    scenario_id: str
    seed_files: tuple[tuple[str, str], ...]
    initialize_git: bool
    git_baseline_files: tuple[tuple[str, str, str], ...]
    mcp_marker: bool
    settle_paths: tuple[str, ...]
    probe_tokens: tuple[str, ...]
    custom_read_external: bool
    hook_exec_fixture: bool
    plan_digest: str


def materialize_permission_fixture(
    scenario: PermissionScenario, /
) -> PermissionFixturePlan:
    if type(scenario) is not PermissionScenario:
        raise ContractDocumentError("permission fixture requires an exact scenario")
    body = {
        "initialize_git": scenario.recipe.initialize_git,
        "git_baseline_files": [
            list(item) for item in scenario.recipe.git_baseline_files
        ],
        "scenario_id": scenario.definition.scenario_id,
        "seed_files": [list(item) for item in scenario.recipe.seed_files],
        "mcp_marker": scenario.recipe.mcp_marker,
        "settle_paths": list(scenario.recipe.settle_paths),
        "probe_tokens": list(scenario.recipe.probe_tokens),
        "custom_read_external": scenario.recipe.custom_read_external,
        "hook_exec_fixture": scenario.recipe.hook_exec_fixture,
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            b"doc2harness.permission-child-fixture/1\0" + canonical_json(body)
        ).hexdigest()
    )
    return PermissionFixturePlan(
        scenario.definition.scenario_id,
        scenario.recipe.seed_files,
        scenario.recipe.initialize_git,
        scenario.recipe.git_baseline_files,
        scenario.recipe.mcp_marker,
        scenario.recipe.settle_paths,
        scenario.recipe.probe_tokens,
        scenario.recipe.custom_read_external,
        scenario.recipe.hook_exec_fixture,
        digest,
    )


__all__ = ["PermissionFixturePlan", "materialize_permission_fixture"]
