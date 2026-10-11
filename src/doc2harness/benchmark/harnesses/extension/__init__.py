"""Typed extension discovery counterfactual Harness."""

from .adapter import (
    ExtensionExecutionPlan,
    build_extension_execution_plan,
)
from .contracts import (
    ExtensionApplicability,
    ExtensionCounterfactualEvaluation,
    ExtensionCounterfactualObservation,
    ExtensionCounterfactualRecipe,
    ExtensionCounterfactualScenario,
    ExtensionCaptureStep,
    ExtensionFace,
    ExtensionFixtureStep,
    ExtensionOutcome,
    ExtensionRunObservation,
    ExtensionScenarioStep,
    ExtensionSetupStep,
    ExtensionToolRoundStep,
    capture_extension_face,
    compile_extension_steps,
    setup_command,
    tool_round,
)
from .fixtures import ExtensionFixturePlan, materialize_extension_fixture
from .oracle import evaluate_extension_counterfactual
from .recipes import SCENARIOS, scenario_by_id

__all__ = [
    "SCENARIOS",
    "ExtensionApplicability",
    "ExtensionCounterfactualEvaluation",
    "ExtensionCounterfactualObservation",
    "ExtensionCounterfactualRecipe",
    "ExtensionCounterfactualScenario",
    "ExtensionCaptureStep",
    "ExtensionExecutionPlan",
    "ExtensionFace",
    "ExtensionFixturePlan",
    "ExtensionFixtureStep",
    "ExtensionOutcome",
    "ExtensionRunObservation",
    "ExtensionScenarioStep",
    "ExtensionSetupStep",
    "ExtensionToolRoundStep",
    "build_extension_execution_plan",
    "capture_extension_face",
    "compile_extension_steps",
    "evaluate_extension_counterfactual",
    "materialize_extension_fixture",
    "scenario_by_id",
    "setup_command",
    "tool_round",
]
