"""Fail-closed oracle for extension discovery counterfactuals."""

from doc2harness.benchmark.definition import ScenarioRole
from doc2harness.contracts.primitives import ContractDocumentError
from doc2harness.runtime.errors import RuntimeBoundaryError

from .contracts import (
    ExtensionApplicability,
    ExtensionCounterfactualEvaluation,
    ExtensionCounterfactualObservation,
    ExtensionCounterfactualObserver,
    ExtensionCounterfactualScenario,
    ExtensionOutcome,
    evaluation_digest,
)


def evaluate_extension_counterfactual(
    scenario: ExtensionCounterfactualScenario,
    observer: ExtensionCounterfactualObserver,
    /,
) -> ExtensionCounterfactualEvaluation:
    if type(scenario) is not ExtensionCounterfactualScenario:
        raise ContractDocumentError("extension oracle requires an exact scenario")
    observation = observer.observe(scenario)
    if type(observation) is not ExtensionCounterfactualObservation:
        raise RuntimeBoundaryError(
            "SBH-EXTENSION-EVIDENCE-TYPE",
            "extension observer returned an invalid evidence type",
        )
    if observation.scenario_id != scenario.definition.scenario_id:
        raise RuntimeBoundaryError(
            "SBH-EXTENSION-EVIDENCE-MISMATCH",
            "extension evidence belongs to another scenario",
        )

    inside = observation.inside
    applicable = ExtensionApplicability.APPLICABLE
    outcome = ExtensionOutcome.FAIL
    reason = "INSIDE_DISCOVERY_FAILED"
    if not inside.fixture_verified:
        applicable = ExtensionApplicability.INCONCLUSIVE
        reason = "INSIDE_FIXTURE_UNVERIFIED"
    elif scenario.definition.role is ScenarioRole.CONTROL:
        if inside.live and inside.request_count >= 2 and inside.seen_faces:
            outcome = ExtensionOutcome.PASS
            reason = "INSIDE_DISCOVERY_OBSERVED"
    else:
        outside = observation.outside
        if outside is None or not outside.fixture_verified:
            applicable = ExtensionApplicability.INCONCLUSIVE
            reason = "OUTSIDE_FIXTURE_UNVERIFIED"
        elif not inside.live or inside.request_count < 2 or not inside.seen_faces:
            applicable = ExtensionApplicability.INCONCLUSIVE
            reason = "INSIDE_COUNTERFACTUAL_UNOBSERVED"
        elif not outside.live or outside.request_count < 2:
            applicable = ExtensionApplicability.INCONCLUSIVE
            reason = "OUTSIDE_COUNTERFACTUAL_UNOBSERVED"
        elif outside.seen_faces:
            reason = "OUTSIDE_DEFINITION_ESCAPED"
        else:
            outcome = ExtensionOutcome.PASS
            reason = "OUTSIDE_DEFINITION_FENCED"

    if applicable is ExtensionApplicability.APPLICABLE and (
        not observation.process_tree_clean or not observation.workspace_clean
    ):
        outcome = ExtensionOutcome.FAIL
        reason = "EXECUTION_CLEANUP_FAILED"
    contribution = (
        scenario.definition.weight_microunits
        if applicable is ExtensionApplicability.APPLICABLE
        else 0
    )
    return ExtensionCounterfactualEvaluation(
        scenario.definition.scenario_id,
        outcome,
        applicable,
        reason,
        evaluation_digest(scenario, observation),
        contribution,
    )


def judge_extension_observation(
    scenario: ExtensionCounterfactualScenario,
    observation: ExtensionCounterfactualObservation,
    /,
) -> ExtensionCounterfactualEvaluation:
    """Project one typed counterfactual observation without running a backend."""

    class _ObservationAdapter:
        def observe(
            self,
            requested: ExtensionCounterfactualScenario,
            /,
        ) -> ExtensionCounterfactualObservation:
            if requested is not scenario:
                raise ContractDocumentError("extension judge scenario identity mismatch")
            return observation

    return evaluate_extension_counterfactual(scenario, _ObservationAdapter())


__all__ = ["evaluate_extension_counterfactual", "judge_extension_observation"]
