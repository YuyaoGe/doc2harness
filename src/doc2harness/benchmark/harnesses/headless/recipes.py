"""Frozen typed recipes for headless CLI scenarios."""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole
from .contracts import (
    FormalProbe,
    HeadlessExecutionMode,
    HeadlessOracleBinding,
    HeadlessProfile,
    HeadlessRecipe,
    JsonOverride,
    SeedFile,
    define_headless_scenario,
)

_ROWS = {
    "headless_p_json_wellformed": (
        HeadlessRecipe(
            profile=HeadlessProfile.JSON_PURE,
            case_digest="sha256:8fca283d6a734d3194ca367d22d13f6d30fee60fb4cc77cb5eb397fe3bcd30c8",
            dependencies=(),
            timeout_seconds=90,
            output_format="json",
            blocking=False,
            reply_content="PUREJSON_8842 finished",
            oracle=HeadlessOracleBinding(sentinel="PUREJSON_8842"),
        ),
        ScenarioRole.CONTROL,
    ),
    "headless_p_json_model_error_wellformed": (
        HeadlessRecipe(
            profile=HeadlessProfile.JSON_ERROR_PURE,
            case_digest="sha256:26ad300b0c8387f54353c7c0fe3c3683bf204943d4c980dec7340f71a7096bb7",
            dependencies=(("headless_p_json_wellformed", DependencyKind.GATE),),
            timeout_seconds=90,
            output_format="json",
            gateway_mode="http_401",
            oracle=HeadlessOracleBinding(),
        ),
        ScenarioRole.TARGET,
    ),
    "p34_ctl_task_text_reaches_wire": (
        HeadlessRecipe(
            profile=HeadlessProfile.PROBE_PRESENT,
            case_digest="sha256:d164292ad645d4f955608491de0ea28cb6901dc41ce26f265e87677544106bca",
            dependencies=(),
            task="ATREFTOK_9934 acknowledge and stop.",
            output_format="text",
            blocking=False,
            reply_content="ack",
            probe_token="ATREFTOK_9934",
            timeout_seconds=90,
            oracle=HeadlessOracleBinding(specification="the task body"),
        ),
        ScenarioRole.CONTROL,
    ),
    "p34_ctl_bare_mention_not_auto_read": (
        HeadlessRecipe(
            profile=HeadlessProfile.PROBE_ABSENT,
            case_digest="sha256:2fadd93ee9d3ec8692ae580cca6bf596050d1caf4158bc2149c3a8efb64972b4",
            dependencies=(),
            task="There is a file called ctx_9934.md in this repo. Do not read it. Say ok and stop.",
            output_format="text",
            blocking=False,
            reply_content="ack",
            probe_token="ATREFTOK_9934",
            seed_files=(SeedFile("ctx_9934.md", "ATREFTOK_9934 the project ships on Tuesdays.\n"),),
            timeout_seconds=90,
            oracle=HeadlessOracleBinding(specification="the content of a bare filename mention"),
        ),
        ScenarioRole.CONTROL,
    ),
    "at_ref_expands_in_headless": (
        HeadlessRecipe(
            profile=HeadlessProfile.PROBE_PRESENT,
            case_digest="sha256:bb1913914002cbc7ccdd2c8bc785af8e73e0af5d6fc6bf70e81bdf23e9eb486a",
            dependencies=(
                ("p34_ctl_bare_mention_not_auto_read", DependencyKind.GATE),
                ("p34_ctl_task_text_reaches_wire", DependencyKind.GATE),
            ),
            task="Summarize @ctx_9934.md in one line and stop.",
            output_format="text",
            reply_content="ack",
            probe_token="ATREFTOK_9934",
            seed_files=(SeedFile("ctx_9934.md", "ATREFTOK_9934 the project ships on Tuesdays.\n"),),
            timeout_seconds=90,
            oracle=HeadlessOracleBinding(specification="the content of an @-referenced file"),
        ),
        ScenarioRole.TARGET,
    ),
    "budget_type_domain_control": (
        HeadlessRecipe(
            profile=HeadlessProfile.BUDGET_TYPE_DOMAIN_CONTROL,
            case_digest="sha256:79ce0a325bfe18c40fe109ec9705d346b5f297ca6e0650f5afe1b2a74ac2f19d",
            blocking=False,
            timeout_seconds=45,
            execution_mode=HeadlessExecutionMode.FORMAL_RUNTIME,
            formal_runtime_probes=(
                FormalProbe(
                    "valid-zero",
                    budget=(
                        JsonOverride("max_steps", "2"),
                        JsonOverride("max_tokens", "0"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
                FormalProbe(
                    "valid-positive",
                    budget=(
                        JsonOverride("max_steps", "97"),
                        JsonOverride("max_tokens", "64"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
            ),
            status_observation_required=True,
        ),
        ScenarioRole.CONTROL,
    ),
    "budget_type_domain": (
        HeadlessRecipe(
            profile=HeadlessProfile.BUDGET_TYPE_DOMAIN,
            case_digest="sha256:c269536e752e1b7c0863711df30b5c08661d8d94ee6b4d1acbb8fc902ed9695a",
            dependencies=(("budget_type_domain_control", DependencyKind.GATE),),
            timeout_seconds=45,
            execution_mode=HeadlessExecutionMode.FORMAL_RUNTIME,
            formal_runtime_probes=(
                FormalProbe(
                    "valid-zero",
                    budget=(
                        JsonOverride("max_steps", "2"),
                        JsonOverride("max_tokens", "0"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
                FormalProbe(
                    "valid-positive",
                    budget=(
                        JsonOverride("max_steps", "97"),
                        JsonOverride("max_tokens", "64"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
                FormalProbe(
                    "invalid-boolean",
                    budget=(
                        JsonOverride("max_steps", "true"),
                        JsonOverride("max_tokens", "0"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
                FormalProbe(
                    "invalid-string",
                    budget=(
                        JsonOverride("max_steps", '"2"'),
                        JsonOverride("max_tokens", "0"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
                FormalProbe(
                    "invalid-float",
                    budget=(
                        JsonOverride("max_steps", "2.5"),
                        JsonOverride("max_tokens", "0"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
                FormalProbe(
                    "invalid-negative",
                    budget=(
                        JsonOverride("max_steps", "-1"),
                        JsonOverride("max_tokens", "0"),
                        JsonOverride("max_wall_seconds", "30"),
                    ),
                ),
            ),
            status_observation_required=True,
        ),
        ScenarioRole.TARGET,
    ),
    "runtime_error_stdout_control": (
        HeadlessRecipe(
            profile=HeadlessProfile.RUNTIME_ERROR_STDOUT_CONTROL,
            case_digest="sha256:e6857e3b3b7693fa9d869c4939036e23acaac9e7aadb2169b887c3ee186f694d",
            blocking=False,
            gateway_mode="seq_probe",
            reply_content="EXACT_REPLY_6047",
            timeout_seconds=40,
            execution_mode=HeadlessExecutionMode.FORMAL_CONTRACT,
            formal_contract_probes=(FormalProbe("model-success", gateway_mode="seq_probe"),),
            status_observation_required=True,
        ),
        ScenarioRole.CONTROL,
    ),
    "runtime_error_stdout": (
        HeadlessRecipe(
            profile=HeadlessProfile.RUNTIME_ERROR_STDOUT,
            case_digest="sha256:1ba625fc10954046643efaa2ab98b5f71346de5a3e5cfe3eb519c2941056a83d",
            dependencies=(("runtime_error_stdout_control", DependencyKind.GATE),),
            gateway_mode="seq_probe",
            reply_content="EXACT_REPLY_6047",
            timeout_seconds=40,
            execution_mode=HeadlessExecutionMode.FORMAL_CONTRACT,
            formal_contract_probes=(
                FormalProbe(
                    "model-runtime-error",
                    gateway_mode="http_401",
                    environment=(("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "1"),),
                ),
            ),
            status_observation_required=True,
        ),
        ScenarioRole.TARGET,
    ),
    "exec_requires_bg_control": (
        HeadlessRecipe(
            profile=HeadlessProfile.EXEC_BACKGROUND,
            case_digest="sha256:3e0f756a0567630227d4125daac829dd8053a59b2b42a254011b3140e7f05575",
            sequence_subcommands=(("--bg", "exec control", "--exec", "touch control.marker"),),
            sequence_pause_seconds=10,
            blocking=False,
            timeout_seconds=60,
            environment=(),
            oracle=HeadlessOracleBinding(
                specification="§4: --exec is valid only as a subordinate argument of --bg; the control must actually write the marker"
            ),
        ),
        ScenarioRole.CONTROL,
    ),
    "exec_requires_bg": (
        HeadlessRecipe(
            profile=HeadlessProfile.EXEC_BACKGROUND,
            case_digest="sha256:3e0f756a0567630227d4125daac829dd8053a59b2b42a254011b3140e7f05575",
            dependencies=(("exec_requires_bg_control", DependencyKind.GATE),),
            sequence_subcommands=(
                ("--bg", "exec control", "--exec", "touch control.marker"),
                ("--exec", "touch target.marker"),
            ),
            sequence_pause_seconds=10,
            blocking=False,
            timeout_seconds=60,
            environment=(),
            oracle=HeadlessOracleBinding(
                specification="§4: --exec without --bg must be refused and must not write target.marker"
            ),
        ),
        ScenarioRole.TARGET,
    ),
    "session_flag_subcommand_conflict_control": (
        HeadlessRecipe(
            profile=HeadlessProfile.SESSION_FLAG_CONFLICT,
            case_digest="sha256:927d74c0d930f19ffeab0df118b5af54d259029ac3741ff07655b7cb6c83fcb2",
            sequence_subcommands=(("memory", "status"), ("config", "list")),
            timeout_seconds=60,
            environment=(),
            oracle=HeadlessOracleBinding(
                specification="The no-resume baseline for management subcommands must yield rc0 on at least one side"
            ),
        ),
        ScenarioRole.CONTROL,
    ),
    "session_flag_subcommand_conflict": (
        HeadlessRecipe(
            profile=HeadlessProfile.SESSION_FLAG_CONFLICT,
            case_digest="sha256:927d74c0d930f19ffeab0df118b5af54d259029ac3741ff07655b7cb6c83fcb2",
            dependencies=(("session_flag_subcommand_conflict_control", DependencyKind.GATE),),
            sequence_subcommands=(
                ("memory", "status"),
                ("memory", "status", "--resume", "NOSUCH_SESSION_9931"),
                ("config", "list"),
                ("config", "list", "--resume", "NOSUCH_SESSION_9931"),
            ),
            timeout_seconds=60,
            environment=(),
            oracle=HeadlessOracleBinding(
                specification="An explicit --resume must not be silently discarded on a management subcommand"
            ),
        ),
        ScenarioRole.TARGET,
    ),
}
SCENARIOS = tuple(
    (define_headless_scenario(test_id, recipe, role) for test_id, (recipe, role) in _ROWS.items())
)
_BY_ID = {scenario.definition.scenario_id: scenario for scenario in SCENARIOS}


def scenario_by_id(scenario_id: str) -> object:
    try:
        return _BY_ID[scenario_id]
    except KeyError:
        raise KeyError(f"unknown headless scenario: {scenario_id}") from None
