"""Purpose: verify that an in-workspace extension definition is discoverable.
1. Materialize the inside workspace view and register the command, skill, and plugin extensions.
2. Send one Bash probe after setup so the extension runtime is exercised.
3. Capture the discovered extension faces through a typed observation.
4. Pass only when discovery succeeds and process/workspace cleanup is clean.
"""

from doc2harness.benchmark.definition import ScenarioRole
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


SCENARIO_ID = "extension_inside_symlink_discovery_control"
STEPS = (
    materialize_symlink_fixture(modes=("inside",)),
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
)

RECIPE = compile_extension_steps(STEPS)

SCENARIO = define_extension_scenario(
    SCENARIO_ID,
    RECIPE,
    ScenarioRole.CONTROL,
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
    elif inside.live and inside.request_count >= 2 and inside.seen_faces:
        outcome = ExtensionOutcome.PASS
        reason = "INSIDE_DISCOVERY_OBSERVED"

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
