"""Validation helpers shared by contract documents."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from doc2harness.contracts.canonical import (
    CanonicalJsonError,
)

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)


class ContractDocumentError(ValueError):
    """Raised when an immutable contract envelope fails closed validation."""


def require_string(value: Any, *, name: str, nfc: bool = True) -> str:
    if not isinstance(value, str):
        raise ContractDocumentError(f"{name} must be a string")
    if nfc and unicodedata.normalize("NFC", value) != value:
        raise ContractDocumentError(f"{name} must already be Unicode NFC")
    return value


def require_digest(value: Any, *, name: str) -> str:
    parsed = require_string(value, name=name, nfc=False)
    if _DIGEST.fullmatch(parsed) is None:
        raise ContractDocumentError(f"{name} is not a v1 SHA-256 digest")
    return parsed


__all__ = [
    "CanonicalJsonError",
    "ContractDocumentError",
]
