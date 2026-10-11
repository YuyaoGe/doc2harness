"""Dispatch one catalog scenario to its reviewed Harness production driver.

This is deliberately a thin dispatcher: family drivers own fixture setup and
typed observations, while this module owns only scenario selection and the
identity-bound event envelope.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path


def _json_value(value: object) -> object:
    """Serialize dataclass and scenario-local NamedTuple judgements uniformly."""

    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "_asdict"):
        return {str(key): _json_value(item) for key, item in value._asdict().items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _driver_module(scenario_module: str) -> str:
    """Map a scenario module to the reviewed driver that can execute it.

    Every family resolves to an in-package dotted path, so an installed wheel
    can execute any of them without a checkout on ``sys.path``.
    """
    if ".benchmark.harnesses.direct." in scenario_module:
        return "doc2harness.runtime.direct_mock_driver"
    if ".benchmark.harnesses.permission." in scenario_module:
        return "doc2harness.runtime.permission_driver"
    if ".benchmark.harnesses.file." in scenario_module:
        return "doc2harness.runtime.file_tool_driver"
    if ".benchmark.harnesses.artifact." in scenario_module:
        return "doc2harness.runtime.artifact_static_driver"
    if ".benchmark.harnesses.state." in scenario_module:
        return "doc2harness.runtime.state_driver"
    if ".benchmark.harnesses.headless." in scenario_module:
        return "doc2harness.runtime.headless_driver"
    if ".benchmark.harnesses.startup." in scenario_module:
        return "doc2harness.runtime.startup_driver"
    if ".benchmark.harnesses.tui." in scenario_module:
        return "doc2harness.runtime.tui_driver"
    if ".benchmark.harnesses.extension." in scenario_module:
        return "doc2harness.runtime.extension_driver"
    raise RuntimeError(f"no reviewed production driver for {scenario_module}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.definition import definition_digest

    scenario_id = os.environ.get("SBH_SCENARIO_ID", "")
    owner = load_scenario_owner(scenario_id)
    module = importlib.import_module(_driver_module(owner.module))
    result = module._run_one()  # family modules expose the reviewed typed boundary
    target = Path(os.environ["SBH_TYPED_EVENT_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "schema": "doc2harness.typed-events/1",
        "source_digest": os.environ["SBH_SOURCE_DIGEST"],
        "task_digest": os.environ["SBH_TASK_DIGEST"],
        "budget_digest": os.environ["SBH_BUDGET_DIGEST"],
        "artifact_digest": os.environ["SBH_ARTIFACT_DIGEST"],
        "artifact_id": os.environ["SBH_ARTIFACT_ID"],
        "scenario_id": scenario_id,
        "scenario_module": owner.module,
        "definition_digest": definition_digest(owner.definition),
        "events": [{
            "type": f"{owner.definition.classification.primary}-observation",
            "observation": _json_value(result["observation"]),
            "judgement": _json_value(result["judgement"]),
        }],
    }, ensure_ascii=False, default=lambda value: value.value if hasattr(value, "value") else str(value), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
