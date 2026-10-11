"""Fail-closed orchestration for a production artifact catalog run.

This module owns the catalog-level boundary only.  A reviewed driver still
owns fixture setup and harness-specific observation production.  Each scenario
is invoked separately, bound to its catalog owner and definition digest, and
must emit one typed-event document; text streams are never evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from doc2harness.benchmark.definition import definition_digest
from doc2harness.benchmark.dispatch import load_scenario_owner
from doc2harness.contracts.primitives import ContractDocumentError
from doc2harness.runtime.artifact_executor import (
    ArtifactExecutionRequest,
    load_typed_event_document,
    run_artifact_driver,
)
from doc2harness.runtime.errors import RuntimeBoundaryError


class CatalogExecutionError(RuntimeBoundaryError):
    """A catalog run failed before it could produce a complete receipt."""


@dataclass(frozen=True, slots=True)
class CatalogExecutionRequest:
    """Inputs for one artifact and an explicit active scenario selection."""

    artifact: Path
    driver_root: Path
    driver_argv: tuple[str, ...]
    output_dir: Path
    artifact_id: str
    source_digest: str
    task_digest: str
    budget_digest: str
    artifact_digest: str
    scenario_ids: tuple[str, ...]
    concurrency: int = 1
    timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not all(isinstance(value, Path) for value in (self.artifact, self.driver_root, self.output_dir)):
            raise ContractDocumentError("catalog execution paths must use Path")
        if type(self.driver_argv) is not tuple or not self.driver_argv:
            raise ContractDocumentError("catalog driver argv must be non-empty")
        if any(type(item) is not str or not item or "\0" in item for item in self.driver_argv):
            raise ContractDocumentError("catalog driver argv contains an invalid item")
        if type(self.artifact_id) is not str or not self.artifact_id or "\0" in self.artifact_id:
            raise ContractDocumentError("catalog artifact id is invalid")
        for value, name in (
            (self.source_digest, "source digest"),
            (self.task_digest, "task digest"),
            (self.budget_digest, "budget digest"),
            (self.artifact_digest, "artifact digest"),
        ):
            if type(value) is not str or not value.startswith("sha256:") or len(value) != 71:
                raise ContractDocumentError(f"catalog {name} is invalid")
        if type(self.scenario_ids) is not tuple or not self.scenario_ids:
            raise ContractDocumentError("catalog scenario selection must be non-empty")
        if any(type(item) is not str or not item or "\0" in item for item in self.scenario_ids):
            raise ContractDocumentError("catalog scenario selection contains an invalid id")
        if len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ContractDocumentError("catalog scenario selection contains duplicates")
        if type(self.concurrency) is not int or self.concurrency < 1:
            raise ContractDocumentError("catalog concurrency must be positive")
        if type(self.timeout_seconds) not in {int, float} or not 0 < self.timeout_seconds <= 3600:
            raise ContractDocumentError("catalog timeout is invalid")


def _scenario_output_path(root: Path, scenario_id: str) -> Path:
    """Return a deterministic path after the ID has passed catalog validation."""

    if "/" in scenario_id or "\\" in scenario_id or scenario_id in {".", ".."}:
        raise CatalogExecutionError("SBH-CATALOG-SCENARIO-ID", "scenario id cannot become a path")
    return root / f"{scenario_id}.json"


def _run_one(request: CatalogExecutionRequest, scenario_id: str, *, environ: Mapping[str, str] | None) -> dict[str, object]:
    owner = load_scenario_owner(scenario_id)
    expected_definition_digest = definition_digest(owner.definition)
    output = _scenario_output_path(request.output_dir, scenario_id)
    run_artifact_driver(
        ArtifactExecutionRequest(
            request.artifact,
            request.driver_root,
            request.driver_argv,
            output,
            request.source_digest,
            request.task_digest,
            request.budget_digest,
            request.artifact_digest,
            request.timeout_seconds,
        ),
        environ={
            **(dict(environ) if environ is not None else {}),
            "SBH_ARTIFACT_ID": request.artifact_id,
            "SBH_SCENARIO_ID": scenario_id,
            "SBH_SCENARIO_MODULE": owner.module,
            "SBH_DEFINITION_DIGEST": expected_definition_digest,
        },
    )
    document = load_typed_event_document(
        output,
        source_digest=request.source_digest,
        task_digest=request.task_digest,
        budget_digest=request.budget_digest,
        artifact_digest=request.artifact_digest,
    )
    if (
        document.get("scenario_id") != scenario_id
        or document.get("scenario_module") != owner.module
        or document.get("definition_digest") != expected_definition_digest
        or document.get("artifact_id") != request.artifact_id
    ):
        raise CatalogExecutionError(
            "SBH-CATALOG-BINDING",
            f"typed observation binding differs for {scenario_id}",
        )
    return {"scenario_id": scenario_id, "scenario_module": owner.module, "document": document}


def run_catalog(
    request: CatalogExecutionRequest,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Execute every selected scenario and write one identity-bound receipt.

    Any failed, missing, duplicate, unknown, or mis-bound scenario aborts the
    receipt.  A partial directory is intentionally not presented as a result.
    """

    request.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []
    failures: list[str] = []
    workers = min(request.concurrency, len(request.scenario_ids))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_one, request, scenario_id, environ=environ): scenario_id
            for scenario_id in request.scenario_ids
        }
        for future in as_completed(futures):
            scenario_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001  # normalize at the catalog boundary
                # Carry the message, not just the class.  Every scenario failure
                # arrives here as ArtifactExecutionError, so the class name alone
                # separates nothing -- and the message is where the driver's own
                # scrubbed stderr tail lives.
                failures.append(f"{scenario_id}:{type(exc).__name__}: {exc}")
    missing = set(request.scenario_ids) - {row["scenario_id"] for row in results}
    if failures or missing:
        # Name what failed: without the scenario ids a run that lost one
        # scenario reads exactly like a product the executor refused outright,
        # and diagnosis has to start by diffing the output directory.  Scenario
        # ids and exception type names are neither host paths nor raw output, so
        # carrying them here keeps the closed-error contract.
        detail = " | ".join(sorted(failures) + sorted(f"{s}:missing" for s in missing))
        raise CatalogExecutionError(
            "SBH-CATALOG-INCOMPLETE",
            "catalog run did not produce one valid bound result for every selected "
            f"scenario ({len(failures) + len(missing)} of {len(request.scenario_ids)}: {detail})",
        )
    results.sort(key=lambda row: str(row["scenario_id"]))
    receipt = {
        "schema": "doc2harness.production-catalog-receipt/1",
        "artifact_id": request.artifact_id,
        "artifact_digest": request.artifact_digest,
        "scenario_count": len(results),
        "rows": results,
    }
    target = request.output_dir / "catalog_receipt.json"
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


__all__ = ["CatalogExecutionError", "CatalogExecutionRequest", "run_catalog"]
