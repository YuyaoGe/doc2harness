"""Typed, non-executing benchmark catalog contracts."""

from doc2harness.benchmark.catalog import (
    CatalogRoot,
    CatalogShard,
    CatalogVerification,
    LogicalCatalogResource,
    load_catalog_sources,
    parse_catalog_root,
    parse_catalog_shard,
    verify_catalog_sources,
)
from doc2harness.benchmark.definition import (
    Classification,
    ComponentKind,
    ComponentRef,
    DependencyKind,
    DependencyRef,
    ExperimentBinding,
    ScenarioDefinition,
    ScenarioDifficulty,
    ScenarioRole,
    UnsupportedPolicy,
    definition_digest,
)
from doc2harness.benchmark.taxonomy import (
    Taxonomy,
    TaxonomyEntry,
    parse_taxonomy,
    validate_classification,
)

__all__ = [
    "CatalogRoot",
    "CatalogShard",
    "CatalogVerification",
    "Classification",
    "ComponentKind",
    "ComponentRef",
    "DependencyKind",
    "DependencyRef",
    "ExperimentBinding",
    "LogicalCatalogResource",
    "ScenarioDefinition",
    "ScenarioDifficulty",
    "ScenarioRole",
    "Taxonomy",
    "TaxonomyEntry",
    "UnsupportedPolicy",
    "definition_digest",
    "load_catalog_sources",
    "parse_catalog_root",
    "parse_catalog_shard",
    "parse_taxonomy",
    "validate_classification",
    "verify_catalog_sources",
]
