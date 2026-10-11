"""Strict canonical JSON primitives for immutable contract documents."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

_MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
_MAX_DEPTH = 64
_MAX_KEYS = 100_000
_MAX_STRING_LENGTH = 1 * 1024 * 1024
_MIN_INTEGER = -(2**63)
_MAX_INTEGER = 2**63 - 1


class CanonicalJsonError(ValueError):
    """Raised when a value is outside the canonical v1 JSON subset."""


def _validate(value: Any, *, depth: int, counters: dict[str, int]) -> None:
    if depth > _MAX_DEPTH:
        raise CanonicalJsonError("maximum nesting depth exceeded")

    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not _MIN_INTEGER <= value <= _MAX_INTEGER:
            raise CanonicalJsonError("integer is outside the signed 64-bit range")
        return
    if isinstance(value, float):
        raise CanonicalJsonError("floating-point values are forbidden")
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            raise CanonicalJsonError("maximum string length exceeded")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise CanonicalJsonError("string is not valid Unicode scalar text") from error
        return
    if isinstance(value, Mapping):
        counters["keys"] += len(value)
        if counters["keys"] > _MAX_KEYS:
            raise CanonicalJsonError("maximum object-key count exceeded")
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalJsonError("object keys must be strings")
            if unicodedata.normalize("NFC", key) != key:
                raise CanonicalJsonError("object keys must already be Unicode NFC")
            _validate(key, depth=depth + 1, counters=counters)
            _validate(item, depth=depth + 1, counters=counters)
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        for item in value:
            _validate(item, depth=depth + 1, counters=counters)
        return
    raise CanonicalJsonError(f"unsupported value type: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    """Encode a value as canonical v1 JSON bytes."""

    _validate(value, depth=0, counters={"keys": 0})
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise CanonicalJsonError("value cannot be encoded as canonical JSON") from error
    if len(encoded) > _MAX_DOCUMENT_BYTES:
        raise CanonicalJsonError("maximum document size exceeded")
    return encoded


__all__ = ["CanonicalJsonError", "canonical_json"]
