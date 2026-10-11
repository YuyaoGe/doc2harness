#!/usr/bin/env python3
"""Shared verdict boundary and AND-gate evaluator for all smoke layers.

Only the pieces the smoke layers import at runtime live here: status
finalization (``finalize_case`` / ``finalize_results``), gate-prerequisite
reading (``case_gates``), AND-gate application (``apply_gates``) and
requested-set closure (``dependency_closure``).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

STATUSES = {"pass", "fail", "na", "error"}
_NA_PREFIXES = ("inconclusive", "n/a", "not applicable", "not-applicable")


class SmokeContractError(RuntimeError):
    pass


def finalize_case(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one case row once, at the layer boundary."""
    row = dict(value)
    explicit = str(row.get("status", "")).strip().lower()
    if explicit and explicit not in STATUSES:
        raise SmokeContractError(
            f"{row.get('id', '<missing>')}: invalid explicit status {explicit!r}"
        )
    if not explicit:
        reason = str(row.get("why", "")).strip().lower()
        if "inconclusive" in reason or reason.startswith(_NA_PREFIXES):
            explicit = "na"
        else:
            explicit = "pass" if row.get("ok") else "fail"
    row["status"] = explicit
    # `ok` is derived from the authoritative status.
    row["ok"] = explicit in {"pass", "na"}
    return row


def finalize_results(values: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = [finalize_case(value) for value in values]
    ids = [row.get("id") for row in rows]
    if any(not test_id for test_id in ids):
        raise SmokeContractError("smoke result missing id")
    if len(ids) != len(set(ids)):
        raise SmokeContractError("duplicate smoke result id")
    return rows


def case_gates(case: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    one = case.get("gate")
    many = case.get("gates")
    if isinstance(one, str):
        refs.append(one)
    if isinstance(many, (list, tuple)):
        refs.extend(str(value) for value in many)
    if len(refs) != len(set(refs)):
        raise SmokeContractError(f"{case.get('id')}: duplicate gate prerequisite")
    return tuple(refs)


def apply_gates(
    cases: Iterable[Mapping[str, Any]],
    values: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Apply AND gates using status only; reason text is never interpreted here."""
    ordered_cases = list(cases)
    rows = {row["id"]: row for row in finalize_results(values)}
    for case in ordered_cases:
        test_id = str(case["id"])
        if test_id not in rows:
            rows[test_id] = {
                "id": test_id,
                "ok": False,
                "blocking": bool(case.get("blocking", True)),
                "status": "error",
                "why": "smoke case produced no result",
            }
    for case in ordered_cases:
        test_id = str(case["id"])
        failed = [
            gate
            for gate in case_gates(case)
            if gate not in rows or rows[gate]["status"] != "pass"
        ]
        if not failed:
            continue
        row = rows[test_id]
        row["status"] = "na"
        row["ok"] = True
        row["gate_status"] = "blocked"
        row["gate_prerequisites"] = list(case_gates(case))
        row["failed_prerequisites"] = failed
        row["why"] = (
            f"gate prerequisites not PASS: {', '.join(failed)}; "
            "dependent explicitly not observable"
        )
    return [rows[str(case["id"])] for case in ordered_cases]


def dependency_closure(
    cases: Iterable[Mapping[str, Any]], requested: Iterable[str]
) -> list[dict[str, Any]]:
    ordered = list(cases)
    by_id = {str(case["id"]): case for case in ordered}
    need = set(requested)
    unknown = need - set(by_id)
    if unknown:
        raise SmokeContractError(f"unknown directed IDs: {sorted(unknown)}")
    frontier = list(need)
    while frontier:
        for gate in case_gates(by_id[frontier.pop()]):
            if gate not in by_id:
                raise SmokeContractError(f"dangling gate prerequisite {gate}")
            if gate not in need:
                need.add(gate)
                frontier.append(gate)
    return [case for case in ordered if str(case["id"]) in need]
