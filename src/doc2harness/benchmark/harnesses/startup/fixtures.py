"""Immutable startup fixture plans."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json

from .contracts import StartupScenario


@dataclass(frozen=True, slots=True)
class StartupFixturePlan:
    config_text: str | None
    home_files: tuple[tuple[str, str], ...]
    environment: tuple[tuple[str, str], ...]
    tripwire: bool
    launcher_probe: str | None
    plan_digest: str


def materialize_startup_fixture(scenario: StartupScenario, /) -> StartupFixturePlan:
    recipe = scenario.recipe
    body = {
        "config_text": recipe.config_text,
        "environment": list(recipe.environment),
        "home_files": list(recipe.home_files),
        "launcher_probe": recipe.launcher_probe,
        "scenario_id": scenario.definition.scenario_id,
        "tripwire": recipe.tripwire,
    }
    digest = "sha256:" + hashlib.sha256(
        b"doc2harness.startup-fixture/1\0" + canonical_json(body)
    ).hexdigest()
    return StartupFixturePlan(
        recipe.config_text,
        recipe.home_files,
        recipe.environment,
        recipe.tripwire,
        recipe.launcher_probe,
        digest,
    )
