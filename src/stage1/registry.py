#!/usr/bin/env python3
"""Persistent pin registry for artifacts: append one JSONL line per build with
run_id + artifact hash + paths + metadata.

The pin index gives reproducible traceability (the same builder re-run should
yield the same hash). The registry file is small; artifact bytes stay in
results/.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# <repo>/artifacts_store/registry.jsonl (registry.py -> stage1 -> src -> repo root)
REGISTRY = Path(__file__).resolve().parents[2] / "artifacts_store" / "registry.jsonl"


def register(entry: dict) -> Path:
    """Append one record (ts filled in automatically). Returns the registry path."""
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(), **entry}
    with REGISTRY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return REGISTRY
