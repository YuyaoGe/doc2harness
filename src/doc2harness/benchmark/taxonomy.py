"""Closed taxonomy types and fail-closed source-loading boundary."""

from __future__ import annotations

import tomllib
import unicodedata
from dataclasses import dataclass
from typing import Any

from doc2harness.benchmark.definition import Classification
from doc2harness.contracts.primitives import ContractDocumentError

_MAX_ENTRIES = 4096
_MAX_SOURCE_BYTES = 1_048_576
_MAX_SOURCE_LINES = 16_384
_MAX_LINE_BYTES = 8_192
_MAX_DEPTH = 5


@dataclass(frozen=True, slots=True)
class TaxonomyEntry:
    function_id: str
    title: str

    def __post_init__(self) -> None:
        Classification(self.function_id)
        if type(self.title) is not str or not self.title or len(self.title) > 256:
            raise ContractDocumentError("taxonomy title must be a bounded non-empty string")
        if unicodedata.normalize("NFC", self.title) != self.title:
            raise ContractDocumentError("taxonomy title must be Unicode NFC")
        if self.title != self.title.strip() or any(
            ord(char) < 32
            or 0x7F <= ord(char) <= 0x9F
            or ord(char) in {0x2028, 0x2029}
            or 0xD800 <= ord(char) <= 0xDFFF
            for char in self.title
        ):
            raise ContractDocumentError("taxonomy title is not canonical")


@dataclass(frozen=True, slots=True)
class Taxonomy:
    entries: tuple[TaxonomyEntry, ...]

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple or not self.entries:
            raise ContractDocumentError("taxonomy must contain an exact non-empty tuple")
        if len(self.entries) > _MAX_ENTRIES:
            raise ContractDocumentError("taxonomy exceeds the entry limit")
        if any(type(item) is not TaxonomyEntry for item in self.entries):
            raise ContractDocumentError("taxonomy entries must use TaxonomyEntry")
        identities = tuple(item.function_id for item in self.entries)
        if identities != tuple(sorted(identities)) or len(set(identities)) != len(identities):
            raise ContractDocumentError("taxonomy identities must be sorted and unique")


def _source_text(raw: bytes, name: str) -> str:
    if type(raw) is not bytes:
        raise ContractDocumentError(f"{name} source must be exact bytes")
    if not raw or len(raw) > _MAX_SOURCE_BYTES:
        raise ContractDocumentError(f"{name} source must be bounded and non-empty")
    if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw or b"\r" in raw:
        raise ContractDocumentError(f"{name} source encoding is not canonical")
    lines = raw.split(b"\n")
    if len(lines) > _MAX_SOURCE_LINES or any(len(line) > _MAX_LINE_BYTES for line in lines):
        raise ContractDocumentError(f"{name} source layout exceeds limits")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ContractDocumentError(f"{name} source is not UTF-8") from None
    if unicodedata.normalize("NFC", text) != text:
        raise ContractDocumentError(f"{name} source must be Unicode NFC")
    return text


def _depth(value: object, level: int = 1) -> int:
    if type(value) in {dict, list} and level > _MAX_DEPTH:
        raise ContractDocumentError("TOML container depth exceeds limit")
    if type(value) is dict:
        for item in value.values():
            _depth(item, level + 1)
    elif type(value) is list:
        for item in value:
            _depth(item, level + 1)
    return level


def _parse_closed_toml(raw: bytes, name: str) -> dict[str, Any]:
    text = _source_text(raw, name)
    try:
        value = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        raise ContractDocumentError(f"{name} source is not valid closed TOML") from None
    if type(value) is not dict:
        raise ContractDocumentError(f"{name} source must be a table")
    _depth(value)
    return value


def _exact_keys(value: object, keys: frozenset[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ContractDocumentError(f"{name} has an invalid closed shape")
    return value


def parse_taxonomy(raw: bytes, /) -> Taxonomy:
    """Parse one closed, canonical taxonomy source."""

    source = _exact_keys(
        _parse_closed_toml(raw, "taxonomy"),
        frozenset({"format", "functions"}),
        "taxonomy",
    )
    if source["format"] != "doc2harness.taxonomy-source/1":
        raise ContractDocumentError("taxonomy format is unsupported")
    functions = source["functions"]
    if type(functions) is not list or not functions or len(functions) > _MAX_ENTRIES:
        raise ContractDocumentError("taxonomy functions must be a bounded non-empty array")
    entries: list[TaxonomyEntry] = []
    for item in functions:
        row = _exact_keys(item, frozenset({"function_id", "title"}), "taxonomy function")
        entries.append(TaxonomyEntry(row["function_id"], row["title"]))
    return Taxonomy(tuple(entries))


def validate_classification(value: Classification, taxonomy: Taxonomy, /) -> None:
    """Validate a typed classification against a closed in-memory taxonomy."""

    if type(value) is not Classification or type(taxonomy) is not Taxonomy:
        raise ContractDocumentError("classification validation requires exact typed values")
    known = frozenset(item.function_id for item in taxonomy.entries)
    unknown = frozenset((value.primary, *value.secondary)) - known
    if unknown:
        raise ContractDocumentError(f"classification contains unknown functions: {sorted(unknown)!r}")


__all__ = [
    "Taxonomy", "TaxonomyEntry", "parse_taxonomy", "validate_classification",
]
