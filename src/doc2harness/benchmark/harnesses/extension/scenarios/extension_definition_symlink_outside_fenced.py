"""Purpose: verify that an outside-workspace extension symlink is not discovered.
1. Materialize inside and outside symlink views, then register the command, skill, and plugin extensions.
2. Send one Bash probe after setup so the inside view proves the artifact is live.
3. Capture extension faces from both views through typed observations.
4. Pass only when inside discovery succeeds, outside discovery is absent, and cleanup is clean.
"""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole
from doc2harness.benchmark.harnesses.extension.contracts import (
    ExtensionApplicability,
    ExtensionFace,
    ExtensionCounterfactualEvaluation,
    ExtensionCounterfactualObservation,
    ExtensionOutcome,
    capture_extension_face,
    compile_extension_steps,
    evaluation_digest,
    define_extension_scenario,
    materialize_symlink_fixture,
    setup_command,
    tool_round,
)
from doc2harness.runtime.errors import RuntimeBoundaryError


SCENARIO_ID = "extension_definition_symlink_outside_fenced"
STEPS = (
    materialize_symlink_fixture(modes=("inside", "outside")),
    setup_command("/mode yolo"),
    setup_command("/extsymcommand"),
    setup_command("/skill extsymskill"),
    setup_command("/extsymplugin"),
    tool_round(tool="bash", command="echo PERM_LIVE_7755 > live.txt"),
    capture_extension_face(
        mode="inside",
        faces=(
            ExtensionFace.COMMANDS,
            ExtensionFace.SKILLS,
            ExtensionFace.HOOKS,
            ExtensionFace.PLUGINS,
        ),
    ),
    capture_extension_face(mode="outside", faces=()),
)

RECIPE = compile_extension_steps(
    STEPS,
    dependencies=(
        ("extension_inside_symlink_discovery_control", DependencyKind.GATE),
    ),
)

SCENARIO = define_extension_scenario(
    SCENARIO_ID,
    RECIPE,
    ScenarioRole.TARGET,
)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(
    observation: ExtensionCounterfactualObservation,
    /,
) -> ExtensionCounterfactualEvaluation:
    """Judge only typed extension facts; never execute a backend here."""

    if type(observation) is not ExtensionCounterfactualObservation:
        raise RuntimeBoundaryError(
            "SBH-EXTENSION-EVIDENCE-TYPE",
            "extension scenario received an invalid evidence type",
        )
    if observation.scenario_id != SCENARIO_ID:
        raise RuntimeBoundaryError(
            "SBH-EXTENSION-EVIDENCE-MISMATCH",
            "extension evidence belongs to another scenario",
        )

    inside = observation.inside
    applicability = ExtensionApplicability.APPLICABLE
    outcome = ExtensionOutcome.FAIL
    reason = "INSIDE_DISCOVERY_FAILED"

    if not inside.fixture_verified:
        applicability = ExtensionApplicability.INCONCLUSIVE
        reason = "INSIDE_FIXTURE_UNVERIFIED"
    else:
        outside = observation.outside
        if outside is None or not outside.fixture_verified:
            applicability = ExtensionApplicability.INCONCLUSIVE
            reason = "OUTSIDE_FIXTURE_UNVERIFIED"
        elif not inside.live or inside.request_count < 2 or not inside.seen_faces:
            applicability = ExtensionApplicability.INCONCLUSIVE
            reason = "INSIDE_COUNTERFACTUAL_UNOBSERVED"
        elif not outside.live or outside.request_count < 2:
            applicability = ExtensionApplicability.INCONCLUSIVE
            reason = "OUTSIDE_COUNTERFACTUAL_UNOBSERVED"
        elif outside.seen_faces:
            reason = "OUTSIDE_DEFINITION_ESCAPED"
        else:
            outcome = ExtensionOutcome.PASS
            reason = "OUTSIDE_DEFINITION_FENCED"

    if applicability is ExtensionApplicability.APPLICABLE and (
        not observation.process_tree_clean or not observation.workspace_clean
    ):
        outcome = ExtensionOutcome.FAIL
        reason = "EXECUTION_CLEANUP_FAILED"

    contribution = (
        SCENARIO.definition.weight_microunits
        if applicability is ExtensionApplicability.APPLICABLE
        else 0
    )
    return ExtensionCounterfactualEvaluation(
        SCENARIO_ID,
        outcome,
        applicability,
        reason,
        evaluation_digest(SCENARIO, observation),
        contribution,
    )


__all__ = ["SCENARIO", "STEPS", "definition", "judge", "scenario"]
