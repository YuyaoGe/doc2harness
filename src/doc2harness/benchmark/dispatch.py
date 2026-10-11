"""Resolve active catalog IDs to their scenario-local execution owners."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Callable

from doc2harness.benchmark.catalog import (
    CatalogRoot,
    load_catalog_sources,
    parse_catalog_root,
)
from doc2harness.benchmark.definition import ScenarioDefinition
from doc2harness.contracts.primitives import ContractDocumentError

_PACKAGE = "doc2harness"
_BENCHMARK = "benchmark"


@dataclass(frozen=True, slots=True)
class ScenarioOwner:
    """One catalog definition and the module that owns its runtime recipe."""

    definition: ScenarioDefinition
    scenario: object
    judge: Callable[[object], object]
    module: str


def _catalog_root() -> CatalogRoot:
    root = files(_PACKAGE).joinpath(_BENCHMARK)
    return parse_catalog_root(root.joinpath("catalog_root.toml").read_bytes())


@lru_cache(maxsize=1)
def _definitions_and_modules() -> tuple[
    tuple[ScenarioDefinition, ...], dict[str, str]
]:
    package = files(_PACKAGE)
    benchmark = package.joinpath(_BENCHMARK)
    root = _catalog_root()
    resources = {
        root.taxonomy_resource: benchmark.joinpath("taxonomy.toml").read_bytes(),
        **{
            shard.resource: package.joinpath(*shard.resource.segments).read_bytes()
            for shard in root.shards
        },
    }
    definitions = load_catalog_sources(
        root,
        resources,
        lambda module, export: getattr(importlib.import_module(module), export),
    )
    modules = {
        getattr(importlib.import_module(module), export).scenario_id: module
        for shard in root.shards
        for module, export in shard.exports
    }
    if len(modules) != len(definitions):
        raise ContractDocumentError("active catalog module index is ambiguous")
    return definitions, modules


def active_scenario_ids() -> tuple[str, ...]:
    """Return the complete, catalog-ordered active scenario universe."""

    definitions, _ = _definitions_and_modules()
    return tuple(item.scenario_id for item in definitions)


def load_scenario_owner(scenario_id: str, /) -> ScenarioOwner:
    """Load one local recipe/judge owner and verify its catalog binding."""

    if type(scenario_id) is not str or not scenario_id:
        raise ContractDocumentError("scenario id must be a non-empty string")
    definitions, modules = _definitions_and_modules()
    definitions_by_id = {item.scenario_id: item for item in definitions}
    definition = definitions_by_id.get(scenario_id)
    module_name = modules.get(scenario_id)
    if definition is None or module_name is None:
        raise ContractDocumentError("scenario id is absent from the active catalog")
    module = importlib.import_module(module_name)
    scenario = getattr(module, "SCENARIO", None)
    alias = getattr(module, "scenario", None)
    judge = getattr(module, "judge", None)
    local_definition = getattr(module, "definition", None)
    if scenario is None or alias is None:
        raise ContractDocumentError("scenario module has no canonical local owner")
    if local_definition is not definition:
        raise ContractDocumentError("scenario module definition differs from catalog import")
    owned_definition = getattr(scenario, "definition", None)
    if owned_definition is not definition:
        raise ContractDocumentError("scenario owner definition differs from catalog import")
    if getattr(alias, "definition", None) is not definition:
        raise ContractDocumentError("catalog compatibility scenario differs from catalog import")
    if not callable(judge):
        raise ContractDocumentError("scenario module has no local judge")
    return ScenarioOwner(definition, scenario, judge, module_name)


__all__ = [
    "ScenarioOwner",
    "active_scenario_ids",
    "load_scenario_owner",
]
