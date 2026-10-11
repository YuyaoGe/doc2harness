"""Explicit catalog sources, verification, and authority-backed sealing."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

from doc2harness.benchmark.definition import (
    DependencyKind,
    ScenarioDefinition,
    ScenarioRole,
    definition_digest,
)
from doc2harness.benchmark.taxonomy import (
    Taxonomy,
    _exact_keys,
    _parse_closed_toml,
    parse_taxonomy,
    validate_classification,
)
from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import (
    ContractDocumentError,
    require_digest,
)

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z", re.ASCII)
_MODULE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z", re.ASCII)
_EXPORT = re.compile(r"[a-z][a-z0-9_]{0,127}\Z", re.ASCII)
_MODULE_PREFIX = "doc2harness.benchmark.harnesses."
_MAX_DEFINITIONS = 4096
_MAX_EXPORTS = 4096
_MAX_SHARDS = 4096
_MAX_SOURCE_BYTES = 1_048_576
_SHARD_DOMAIN = b"doc2harness.catalog-shard/1\x00"
_ROOT_DOMAIN = b"doc2harness.catalog-root/1\x00"
_VERIFICATION_DOMAIN = b"doc2harness.catalog-verification/1\x00"


@dataclass(frozen=True, slots=True)
class LogicalCatalogResource:
    """A package-relative resource identity, never a host filesystem path."""

    segments: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.segments) is not tuple or not self.segments or len(self.segments) > 32:
            raise ContractDocumentError("catalog resource must have bounded exact segments")
        for segment in self.segments:
            if type(segment) is not str or _SEGMENT.fullmatch(segment) is None:
                raise ContractDocumentError("catalog resource contains an unsafe segment")
            if segment in {".", ".."} or segment.startswith("~") or ":" in segment:
                raise ContractDocumentError("catalog resource contains an unsafe segment")


def _digest(value: object, name: str) -> str:
    try:
        return require_digest(value, name=name)
    except ContractDocumentError:
        raise


def _raw_digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _resource(value: object, name: str) -> LogicalCatalogResource:
    if type(value) is not str or not value or value.startswith(("/", "~")):
        raise ContractDocumentError(f"{name} is not a canonical relative resource")
    if (
        value.endswith("/")
        or "//" in value
        or "\\" in value
        or ":" in value
        or "%" in value
        or not value.isascii()
    ):
        raise ContractDocumentError(f"{name} is not a canonical relative resource")
    return LogicalCatalogResource(tuple(value.split("/")))


def _module_export(value: object, name: str) -> tuple[str, str]:
    row = _exact_keys(value, frozenset({"module", "export"}), name)
    module = row["module"]
    export = row["export"]
    if (
        type(module) is not str
        or len(module) > 255
        or _MODULE.fullmatch(module) is None
        or not module.startswith(_MODULE_PREFIX)
    ):
        raise ContractDocumentError(f"{name} module is not canonical")
    if type(export) is not str or _EXPORT.fullmatch(export) is None:
        raise ContractDocumentError(f"{name} export is not canonical")
    return module, export


def _exports(value: object, name: str) -> tuple[tuple[str, str], ...]:
    if type(value) is not list or not value or len(value) > _MAX_EXPORTS:
        raise ContractDocumentError(f"{name} must be a bounded non-empty array")
    result = tuple(_module_export(item, name) for item in value)
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise ContractDocumentError(f"{name} must be sorted and unique")
    return result


def _shard_semantic_body(
    function_id: str,
    resource: LogicalCatalogResource,
    exports: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    return {
        "exports": [
            {"export": export, "module": module} for module, export in exports
        ],
        "function_id": function_id,
        "resource": list(resource.segments),
    }


def _semantic_digest(domain: bytes, body: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(domain + canonical_json(body)).hexdigest()


@dataclass(frozen=True, slots=True)
class CatalogShard:
    function_id: str
    resource: LogicalCatalogResource
    exports: tuple[tuple[str, str], ...]
    source_digest: str
    semantic_digest: str

    def __post_init__(self) -> None:
        if type(self.function_id) is not str or _IDENTIFIER.fullmatch(self.function_id) is None:
            raise ContractDocumentError("shard function id is not canonical")
        if type(self.resource) is not LogicalCatalogResource:
            raise ContractDocumentError("shard resource must be a logical resource")
        expected_resource = (
            "benchmark",
            "harnesses",
            *self.function_id.split("."),
            "catalog.toml",
        )
        if self.resource.segments != expected_resource:
            raise ContractDocumentError("shard resource does not match its function")
        if type(self.exports) is not tuple or not self.exports or len(self.exports) > _MAX_EXPORTS:
            raise ContractDocumentError("shard exports must be a bounded exact tuple")
        for item in self.exports:
            if type(item) is not tuple or len(item) != 2:
                raise ContractDocumentError("shard exports require exact pairs")
            module, export = item
            _module_export({"module": module, "export": export}, "shard export")
        if self.exports != tuple(sorted(self.exports)) or len(set(self.exports)) != len(self.exports):
            raise ContractDocumentError("shard exports must be sorted and unique")
        _digest(self.source_digest, "shard source digest")
        _digest(self.semantic_digest, "shard semantic digest")
        expected_semantic = _semantic_digest(
            _SHARD_DOMAIN,
            _shard_semantic_body(self.function_id, self.resource, self.exports),
        )
        if self.semantic_digest != expected_semantic:
            raise ContractDocumentError("shard semantic digest differs from its fields")


@dataclass(frozen=True, slots=True)
class CatalogRoot:
    taxonomy_resource: LogicalCatalogResource
    taxonomy_digest: str
    shards: tuple[CatalogShard, ...]
    source_digest: str
    semantic_digest: str

    def __post_init__(self) -> None:
        if (
            type(self.taxonomy_resource) is not LogicalCatalogResource
            or self.taxonomy_resource.segments != ("benchmark", "taxonomy.toml")
        ):
            raise ContractDocumentError("taxonomy resource is not canonical")
        _digest(self.taxonomy_digest, "taxonomy digest")
        if type(self.shards) is not tuple or not self.shards or len(self.shards) > _MAX_SHARDS:
            raise ContractDocumentError("catalog root must contain a bounded exact shard tuple")
        if any(type(item) is not CatalogShard for item in self.shards):
            raise ContractDocumentError("catalog root shards must use CatalogShard")
        identities = tuple(item.function_id for item in self.shards)
        resources = tuple(item.resource for item in self.shards)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ContractDocumentError("catalog root shard functions must be sorted and unique")
        if len(set(resources)) != len(resources):
            raise ContractDocumentError("catalog root shard resources must be unique")
        export_count = 0
        seen_exports: set[tuple[str, str]] = set()
        for shard in self.shards:
            for export in shard.exports:
                export_count += 1
                if export_count > _MAX_EXPORTS:
                    raise ContractDocumentError("catalog root exports exceed the total limit")
                if export in seen_exports:
                    raise ContractDocumentError("catalog root exports must be globally unique")
                seen_exports.add(export)
        _digest(self.source_digest, "root source digest")
        _digest(self.semantic_digest, "root semantic digest")
        expected_semantic = _semantic_digest(
            _ROOT_DOMAIN,
            _root_body(self.taxonomy_resource, self.taxonomy_digest, self.shards),
        )
        if self.semantic_digest != expected_semantic:
            raise ContractDocumentError("root semantic digest differs from its fields")


@dataclass(frozen=True, slots=True)
class CatalogVerification:
    definitions: tuple[ScenarioDefinition, ...]
    source_digests: tuple[str, ...]
    semantic_digest: str

    def __post_init__(self) -> None:
        if type(self.definitions) is not tuple or not self.definitions:
            raise ContractDocumentError("catalog verification requires definitions")
        if any(type(item) is not ScenarioDefinition for item in self.definitions):
            raise ContractDocumentError("verified definitions must use ScenarioDefinition")
        if type(self.source_digests) is not tuple or not self.source_digests:
            raise ContractDocumentError("catalog verification requires source digests")
        for item in self.source_digests:
            _digest(item, "source digest")
        _digest(self.semantic_digest, "catalog semantic digest")


CatalogImporter = Callable[[str, str], ScenarioDefinition]


def _root_body(
    taxonomy_resource: LogicalCatalogResource,
    taxonomy_digest: str,
    shards: tuple[CatalogShard, ...],
) -> dict[str, object]:
    return {
        "shards": [
            {
                "exports": [
                    {"export": export, "module": module}
                    for module, export in shard.exports
                ],
                "function_id": shard.function_id,
                "resource": list(shard.resource.segments),
                "semantic_digest": shard.semantic_digest,
                "source_digest": shard.source_digest,
            }
            for shard in shards
        ],
        "taxonomy_resource": list(taxonomy_resource.segments),
        "taxonomy_source_digest": taxonomy_digest,
    }


def parse_catalog_root(raw: bytes, /) -> CatalogRoot:
    source = _exact_keys(
        _parse_closed_toml(raw, "catalog root"),
        frozenset({"format", "taxonomy_resource", "taxonomy_source_digest", "shards"}),
        "catalog root",
    )
    if source["format"] != "doc2harness.catalog-root-source/1":
        raise ContractDocumentError("catalog root format is unsupported")
    taxonomy_resource = _resource(source["taxonomy_resource"], "taxonomy resource")
    taxonomy_digest = _digest(source["taxonomy_source_digest"], "taxonomy source digest")
    raw_shards = source["shards"]
    if type(raw_shards) is not list or not raw_shards or len(raw_shards) > _MAX_SHARDS:
        raise ContractDocumentError("catalog root shards must be a bounded non-empty array")
    shards: list[CatalogShard] = []
    total_exports = 0
    for item in raw_shards:
        row = _exact_keys(
            item,
            frozenset({"function_id", "resource", "source_digest", "semantic_digest", "exports"}),
            "catalog root shard",
        )
        exports = _exports(row["exports"], "catalog root exports")
        total_exports += len(exports)
        if total_exports > _MAX_EXPORTS:
            raise ContractDocumentError("catalog root exports exceed the total limit")
        shards.append(
            CatalogShard(
                row["function_id"],
                _resource(row["resource"], "shard resource"),
                exports,
                _digest(row["source_digest"], "shard source digest"),
                _digest(row["semantic_digest"], "shard semantic digest"),
            )
        )
    typed_shards = tuple(shards)
    semantic = _semantic_digest(
        _ROOT_DOMAIN,
        _root_body(taxonomy_resource, taxonomy_digest, typed_shards),
    )
    return CatalogRoot(
        taxonomy_resource,
        taxonomy_digest,
        typed_shards,
        _raw_digest(raw),
        semantic,
    )


def parse_catalog_shard(raw: bytes, /) -> CatalogShard:
    source = _exact_keys(
        _parse_closed_toml(raw, "catalog shard"),
        frozenset({"format", "function_id", "resource", "exports"}),
        "catalog shard",
    )
    if source["format"] != "doc2harness.catalog-shard-source/1":
        raise ContractDocumentError("catalog shard format is unsupported")
    function_id = source["function_id"]
    resource = _resource(source["resource"], "shard resource")
    exports = _exports(source["exports"], "catalog shard exports")
    semantic = _semantic_digest(
        _SHARD_DOMAIN,
        _shard_semantic_body(function_id, resource, exports),
    )
    return CatalogShard(function_id, resource, exports, _raw_digest(raw), semantic)


def _ordered_definitions(
    definitions: tuple[ScenarioDefinition, ...],
) -> tuple[dict[str, int], dict[str, ScenarioDefinition]]:
    if (
        type(definitions) is not tuple
        or not definitions
        or len(definitions) > _MAX_DEFINITIONS
        or any(type(item) is not ScenarioDefinition for item in definitions)
    ):
        raise ContractDocumentError("definitions must be a bounded non-empty exact tuple")
    order: dict[str, int] = {}
    by_id: dict[str, ScenarioDefinition] = {}
    for ordinal, definition in enumerate(definitions):
        if definition.scenario_id in order:
            raise ContractDocumentError("scenario ids must be globally unique")
        order[definition.scenario_id] = ordinal
        by_id[definition.scenario_id] = definition
    graph: dict[str, set[str]] = {}
    for definition in definitions:
        dependency_ids = tuple(item.scenario_id for item in definition.dependencies)
        if len(set(dependency_ids)) != len(dependency_ids):
            raise ContractDocumentError("a scenario cannot reference one id with multiple kinds")
        unknown = set(dependency_ids) - set(order)
        if unknown:
            raise ContractDocumentError("scenario dependency is unknown")
        for dependency in definition.dependencies:
            target = by_id[dependency.scenario_id]
            if dependency.kind in {DependencyKind.CONTROL, DependencyKind.GATE} and (
                target.role is not ScenarioRole.CONTROL or target.weight_microunits != 0
            ):
                raise ContractDocumentError("control and gate dependencies require controls")
        graph[definition.scenario_id] = set(dependency_ids)
    remaining = {node: len(edges) for node, edges in graph.items()}
    dependents: dict[str, set[str]] = {node: set() for node in graph}
    for node, edges in graph.items():
        for edge in edges:
            dependents[edge].add(node)
    ready = [node for node, count in remaining.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for dependent in dependents[node]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if visited != len(graph):
        raise ContractDocumentError("scenario dependency graph contains a cycle")
    return order, by_id


def verify_catalog_sources(
    root: CatalogRoot,
    definitions: tuple[ScenarioDefinition, ...],
    /,
) -> CatalogVerification:
    if type(root) is not CatalogRoot:
        raise ContractDocumentError("catalog root must use the exact typed structure")
    flattened = tuple(
        (shard.function_id, export)
        for shard in root.shards
        for export in shard.exports
    )
    if type(definitions) is not tuple or len(definitions) != len(flattened):
        raise ContractDocumentError("definition count does not match catalog exports")
    _ordered_definitions(definitions)
    for definition, (function_id, _export) in zip(definitions, flattened, strict=True):
        if definition.classification.primary != function_id:
            raise ContractDocumentError("definition primary classification differs from owner shard")
    digests = tuple(definition_digest(item) for item in definitions)
    semantic = _semantic_digest(
        _VERIFICATION_DOMAIN,
        {"definition_digests": list(digests), "root_semantic_digest": root.semantic_digest},
    )
    source_digests = (
        root.source_digest,
        root.taxonomy_digest,
        *(shard.source_digest for shard in root.shards),
    )
    return CatalogVerification(definitions, source_digests, semantic)


def load_catalog_sources(
    root: CatalogRoot,
    resources: dict[LogicalCatalogResource, bytes],
    importer: CatalogImporter,
    /,
) -> tuple[ScenarioDefinition, ...]:
    if type(root) is not CatalogRoot or type(resources) is not dict or not callable(importer):
        raise ContractDocumentError("catalog loading requires exact root, dict resources and importer")
    if len(resources) > _MAX_SHARDS + 1:
        raise ContractDocumentError("catalog resources exceed the size limit")
    if any(type(key) is not LogicalCatalogResource or type(value) is not bytes for key, value in resources.items()):
        raise ContractDocumentError("catalog resources require exact logical keys and byte values")
    if any(not value or len(value) > _MAX_SOURCE_BYTES for value in resources.values()):
        raise ContractDocumentError("catalog resource bytes exceed source limits")
    snapshot = dict(resources)
    expected = {root.taxonomy_resource, *(shard.resource for shard in root.shards)}
    if set(snapshot) != expected:
        raise ContractDocumentError("catalog resource set does not match the root")
    taxonomy_raw = snapshot[root.taxonomy_resource]
    if _raw_digest(taxonomy_raw) != root.taxonomy_digest:
        raise ContractDocumentError("taxonomy source digest differs from the root")
    taxonomy: Taxonomy = parse_taxonomy(taxonomy_raw)
    known_functions = frozenset(item.function_id for item in taxonomy.entries)
    if any(shard.function_id not in known_functions for shard in root.shards):
        raise ContractDocumentError("catalog root contains an unknown taxonomy function")
    parsed_shards: list[CatalogShard] = []
    for expected_shard in root.shards:
        raw = snapshot[expected_shard.resource]
        if _raw_digest(raw) != expected_shard.source_digest:
            raise ContractDocumentError("shard source digest differs from the root")
        parsed = parse_catalog_shard(raw)
        if parsed != expected_shard:
            raise ContractDocumentError("shard source differs from its root descriptor")
        parsed_shards.append(parsed)
    definitions: list[ScenarioDefinition] = []
    object_ids: set[int] = set()
    for shard in parsed_shards:
        for module, export in shard.exports:
            try:
                definition = importer(module, export)
            except Exception:
                raise ContractDocumentError("catalog importer failed") from None
            if type(definition) is not ScenarioDefinition:
                raise ContractDocumentError("catalog importer returned an invalid definition")
            if id(definition) in object_ids:
                raise ContractDocumentError("catalog importer reused a definition object")
            object_ids.add(id(definition))
            if definition.classification.primary != shard.function_id:
                raise ContractDocumentError("imported definition differs from its owner shard")
            validate_classification(definition.classification, taxonomy)
            definitions.append(definition)
    return verify_catalog_sources(root, tuple(definitions)).definitions


__all__ = [
    "CatalogImporter",
    "CatalogRoot",
    "CatalogShard",
    "CatalogVerification",
    "LogicalCatalogResource",
    "load_catalog_sources",
    "parse_catalog_root",
    "parse_catalog_shard",
    "verify_catalog_sources",
]
