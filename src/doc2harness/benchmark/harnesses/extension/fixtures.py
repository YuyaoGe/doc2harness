"""Closed extension definition fixtures for inward and outward symlinks."""

import hashlib
from dataclasses import dataclass

from doc2harness.contracts.canonical import canonical_json
from doc2harness.contracts.primitives import ContractDocumentError

from .contracts import ExtensionCounterfactualScenario, ExtensionFace

WIRE_MARKERS = (
    (ExtensionFace.COMMANDS, "EXTSYM_COMMAND_BODY_9417"),
    (ExtensionFace.SKILLS, "EXTSYM_SKILL_BODY_9418"),
    (ExtensionFace.PLUGINS, "EXTSYM_PLUGIN_BODY_9419"),
)
EXECUTION_MARKERS = (
    (ExtensionFace.COMMANDS, "EXTSYM_COMMAND_EXEC_9420"),
    (ExtensionFace.HOOKS, "EXTSYM_HOOK_EXEC_9421"),
    (ExtensionFace.PLUGINS, "EXTSYM_PLUGIN_EXEC_9422"),
)
LINK_PATHS = (
    (ExtensionFace.COMMANDS, "commands/extsymcommand.md"),
    (ExtensionFace.SKILLS, "skills/extsymskill/SKILL.md"),
    (ExtensionFace.HOOKS, "hooks.json"),
    (ExtensionFace.PLUGINS, "plugins/extsympackage/plugin.json"),
)


@dataclass(frozen=True, slots=True)
class ExtensionFixturePlan:
    scenario_id: str
    modes: tuple[str, ...]
    link_paths: tuple[tuple[ExtensionFace, str], ...]
    wire_markers: tuple[tuple[ExtensionFace, str], ...]
    execution_markers: tuple[tuple[ExtensionFace, str], ...]
    plan_digest: str

    def __post_init__(self) -> None:
        if self.modes not in {("inside",), ("inside", "outside")}:
            raise ContractDocumentError("extension fixture modes are invalid")


def materialize_extension_fixture(
    scenario: ExtensionCounterfactualScenario,
    /,
) -> ExtensionFixturePlan:
    if type(scenario) is not ExtensionCounterfactualScenario:
        raise ContractDocumentError("extension fixture requires an exact scenario")
    modes = ("inside", "outside") if scenario.recipe.paired else ("inside",)
    body = {
        "execution_markers": [(face.value, marker) for face, marker in EXECUTION_MARKERS],
        "link_paths": [(face.value, path) for face, path in LINK_PATHS],
        "modes": list(modes),
        "scenario_id": scenario.definition.scenario_id,
        "wire_markers": [(face.value, marker) for face, marker in WIRE_MARKERS],
    }
    digest = "sha256:" + hashlib.sha256(
        b"doc2harness.extension-counterfactual-fixture/1\0" + canonical_json(body)
    ).hexdigest()
    return ExtensionFixturePlan(
        scenario.definition.scenario_id,
        modes,
        LINK_PATHS,
        WIRE_MARKERS,
        EXECUTION_MARKERS,
        digest,
    )


__all__ = [
    "EXECUTION_MARKERS",
    "LINK_PATHS",
    "WIRE_MARKERS",
    "ExtensionFixturePlan",
    "materialize_extension_fixture",
]
