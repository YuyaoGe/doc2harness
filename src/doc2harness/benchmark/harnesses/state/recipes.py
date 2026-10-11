"""Frozen recipes for child-process state scenarios."""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole
from .contracts import StateOracleProfile, StateRecipe, define_state_scenario

_ROWS = {}
_ROWS.update(
    {
        "security_review_covers_branch_commits": {
            "commands": ("/security-review",),
            "seed_files": (("vuln_9601.py", "import os\n"),),
            "config_keys": (),
            "observed_files": (),
            "profile": "REQUEST_TOKEN_PRESENT",
            "oracle_parameters": (("token", "SECREVTOK_9601"),),
            "execution_parameters": (
                ("ensure_exec", True),
                ("git_branch_commit", True),
                ("git_init", True),
                (
                    "post_files",
                    (("vuln_9601.py", "import os\n# SECREVTOK_9601\nos.system(input())  # obvious smell\n"),),
                ),
                ("stub", (("probe_tokens", ("SECREVTOK_9601",)),)),
            ),
            "dependencies": (("p30_ctl_security_review_sees_worktree_change", "GATE"),),
            "timeout": 60,
            "role": "TARGET",
        },
        "p30_ctl_security_review_sees_worktree_change": {
            "commands": ("/security-review",),
            "seed_files": (("vuln_9601.py", "import os\n"),),
            "config_keys": (),
            "observed_files": (),
            "profile": "REQUEST_TOKEN_PRESENT",
            "oracle_parameters": (("token", "SECREVTOK_9601"),),
            "execution_parameters": (
                ("ensure_exec", True),
                ("git_init", True),
                (
                    "post_files",
                    (("vuln_9601.py", "import os\n# SECREVTOK_9601\nos.system(input())  # obvious smell\n"),),
                ),
                ("stub", (("probe_tokens", ("SECREVTOK_9601",)),)),
            ),
            "dependencies": (),
            "timeout": 60,
            "role": "CONTROL",
        },
    }
)
_PLUGIN_ROWS = {
    "plugin_declared_command_reaches_wire": {
        "commands": ("/probecmd", "please continue"),
        "seed_files": (
            ("plugins/probeplug/commands/probecmd.md", "Summarize PLUGCMD_5150 for the team now.\n"),
            (
                "plugins/probeplug/plugin.json",
                '{\n "name": "probeplug",\n "description": "probe plugin",\n "commands": [\n  "commands/probecmd.md"\n ]\n}',
            ),
        ),
        "config_keys": (),
        "observed_files": (),
        "profile": "CUSTOM_COMMAND_FACE",
        "oracle_parameters": (("token", "PLUGCMD_5150"),),
        "execution_parameters": (("stub", (("probe_tokens", ("PLUGCMD_5150",)),)),),
        "dependencies": (("plugin_toplevel_command_control", "GATE"),),
        "timeout": 60,
        "role": "TARGET",
    },
    "plugin_toplevel_command_control": {
        "commands": ("/probecmd", "please continue"),
        "seed_files": (("commands/probecmd.md", "Summarize PLUGCMD_5150 for the team now.\n"),),
        "config_keys": (),
        "observed_files": (),
        "profile": "CUSTOM_COMMAND_FACE",
        "oracle_parameters": (("token", "PLUGCMD_5150"),),
        "execution_parameters": (("stub", (("probe_tokens", ("PLUGCMD_5150",)),)),),
        "dependencies": (),
        "timeout": 60,
        "role": "CONTROL",
    },
}
_CHILD_STATE_ROWS = {
    "child_add_dir_inheritance_control": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "child_add_dir_inheritance_control"),),
        "execution_parameters": (
            ("kind", "control"),
            (
                "stub",
                (
                    ("mode", "seq_probe"),
                    ("probe_tokens", ("CHILD_PARENT_7324", "CHILD_EMPTY_7326")),
                    (
                        "rounds",
                        (
                            (
                                (("args", (("file_path", "__REPO__/main.txt"),)), ("name", "read")),
                                (("args", (("file_path", "__EXTRA__/extra.txt"),)), ("name", "read")),
                                (
                                    ("args", (("file_path", "__UNAUTHORIZED__/outside.txt"),)),
                                    ("name", "read"),
                                ),
                            ),
                            ((("name", "__finish__"),),),
                        ),
                    ),
                ),
            ),
            (
                "api_probe_variants",
                (
                    (
                        ("actor", "parent"),
                        ("add_dir", True),
                        ("extra_token", "EXTRA_7322"),
                        ("label", "parent-grant"),
                        ("main_token", "MAIN_7321"),
                        ("operation", "child"),
                        ("prompt", "CHILD_PARENT_7324"),
                        ("shape", "parent-grant"),
                        ("unauthorized_token", "UNAUTH_7323"),
                    ),
                    (
                        ("actor", "child"),
                        ("add_dir", False),
                        ("extra_token", "EXTRA_7322"),
                        ("label", "child-empty"),
                        ("main_token", "MAIN_7321"),
                        ("operation", "child"),
                        ("prompt", "CHILD_EMPTY_7326"),
                        ("settle_s", "2.0"),
                        ("shape", "child-empty"),
                        ("unauthorized_token", "UNAUTH_7323"),
                    ),
                ),
            ),
        ),
        "dependencies": (),
        "timeout": 30,
        "role": "CONTROL",
    },
    "child_add_dir_inheritance": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "child_add_dir_inheritance"),),
        "execution_parameters": (
            (
                "stub",
                (
                    ("mode", "seq_probe"),
                    ("probe_tokens", ("CHILD_TARGET_7325", "CHILD_EMPTY_7326")),
                    (
                        "rounds",
                        (
                            (
                                (("args", (("file_path", "__REPO__/main.txt"),)), ("name", "read")),
                                (("args", (("file_path", "__EXTRA__/extra.txt"),)), ("name", "read")),
                                (
                                    ("args", (("file_path", "__UNAUTHORIZED__/outside.txt"),)),
                                    ("name", "read"),
                                ),
                            ),
                            ((("name", "__finish__"),),),
                        ),
                    ),
                ),
            ),
            (
                "api_probe_variants",
                (
                    (
                        ("actor", "child"),
                        ("add_dir", True),
                        ("extra_token", "EXTRA_7322"),
                        ("label", "child-target"),
                        ("main_token", "MAIN_7321"),
                        ("operation", "child"),
                        ("prompt", "CHILD_TARGET_7325"),
                        ("settle_s", "2.0"),
                        ("shape", "child-target"),
                        ("unauthorized_token", "UNAUTH_7323"),
                    ),
                    (
                        ("actor", "child"),
                        ("add_dir", False),
                        ("extra_token", "EXTRA_7322"),
                        ("label", "child-empty-target"),
                        ("main_token", "MAIN_7321"),
                        ("operation", "child"),
                        ("prompt", "CHILD_EMPTY_7326"),
                        ("settle_s", "2.0"),
                        ("shape", "child-empty-target"),
                        ("unauthorized_token", "UNAUTH_7323"),
                    ),
                ),
            ),
        ),
        "dependencies": (("child_add_dir_inheritance_control", "GATE"),),
        "timeout": 30,
        "role": "TARGET",
    },
    "memory_query_topk_residency_control": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "memory_query_topk_residency_control"),),
        "execution_parameters": (
            ("kind", "control"),
            (
                "clipboard_api_variants",
                (
                    (
                        ("decoy", "DECOY_"),
                        ("entries", 2000),
                        ("label", "control"),
                        ("markers", ("MEMORY_ALPHA_6402", "MEMORY_BETA_6403", "MEMORY_GAMMA_6404")),
                        ("operation", "memory"),
                        ("rss_probe_bytes", 16777216),
                        (
                            "stub",
                            (
                                ("mode", "seq_probe"),
                                ("probe_tokens", ("MEMORY_TARGET_ROUTE_6406",)),
                                (
                                    "rounds",
                                    (
                                        (
                                            (
                                                (
                                                    "args",
                                                    (("limit", 3), ("query", "zirconium quasar heliotrope")),
                                                ),
                                                ("name", ("memory_search", "memory-search", "memorysearch")),
                                            ),
                                        ),
                                        ((("name", "__finish__"),),),
                                    ),
                                ),
                            ),
                        ),
                        ("target_prompt", "MEMORY_TARGET_ROUTE_6406"),
                        ("warm_query", "warmup_only_6401"),
                    ),
                ),
            ),
        ),
        "dependencies": (),
        "timeout": 120,
        "role": "CONTROL",
    },
    "memory_query_topk_residency": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "memory_query_topk_residency"),),
        "execution_parameters": (
            (
                "clipboard_api_variants",
                (
                    (
                        ("decoy", "DECOY_"),
                        ("entries", 2000),
                        ("label", "small"),
                        ("markers", ("MEMORY_ALPHA_6402", "MEMORY_BETA_6403", "MEMORY_GAMMA_6404")),
                        ("operation", "memory"),
                        ("rss_probe_bytes", 16777216),
                        (
                            "stub",
                            (
                                ("mode", "seq_probe"),
                                ("probe_tokens", ("MEMORY_TARGET_ROUTE_6406",)),
                                (
                                    "rounds",
                                    (
                                        (
                                            (
                                                (
                                                    "args",
                                                    (("limit", 3), ("query", "zirconium quasar heliotrope")),
                                                ),
                                                ("name", ("memory_search", "memory-search", "memorysearch")),
                                            ),
                                        ),
                                        ((("name", "__finish__"),),),
                                    ),
                                ),
                            ),
                        ),
                        ("target_prompt", "MEMORY_TARGET_ROUTE_6406"),
                        ("warm_query", "warmup_only_6401"),
                    ),
                    (
                        ("decoy", "DECOY_"),
                        ("entries", 120000),
                        ("label", "large"),
                        ("markers", ("MEMORY_ALPHA_6402", "MEMORY_BETA_6403", "MEMORY_GAMMA_6404")),
                        ("operation", "memory"),
                        ("rss_probe_bytes", 16777216),
                        (
                            "stub",
                            (
                                ("mode", "seq_probe"),
                                ("probe_tokens", ("MEMORY_TARGET_ROUTE_6406",)),
                                (
                                    "rounds",
                                    (
                                        (
                                            (
                                                (
                                                    "args",
                                                    (("limit", 3), ("query", "zirconium quasar heliotrope")),
                                                ),
                                                ("name", ("memory_search", "memory-search", "memorysearch")),
                                            ),
                                        ),
                                        ((("name", "__finish__"),),),
                                    ),
                                ),
                            ),
                        ),
                        ("target_prompt", "MEMORY_TARGET_ROUTE_6406"),
                        ("warm_query", "warmup_only_6401"),
                    ),
                ),
            ),
        ),
        "dependencies": (("memory_query_topk_residency_control", "GATE"),),
        "timeout": 180,
        "role": "TARGET",
    },
    "agent_api_close_retry_incomplete_control": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "agent_api_close_retry_incomplete_control"),),
        "execution_parameters": (
            ("kind", "control"),
            (
                "clipboard_api_variants",
                (
                    (
                        ("heartbeat_file", "close_helper.heartbeat"),
                        ("inject_fault", False),
                        ("label", "control"),
                        ("lease_file", "close_helper.lease"),
                        ("operation", "close"),
                        ("pid_file", "close_helper.pid"),
                        ("prompt", "CLOSE_ROUTE_6501"),
                        ("state_file", "close_helper.state"),
                        (
                            "stub",
                            (
                                ("mode", "seq_probe"),
                                ("probe_tokens", ("CLOSE_ROUTE_6501",)),
                                (
                                    "rounds",
                                    (
                                        (
                                            (
                                                ("args", (("run_in_background", True),)),
                                                ("command", "__CLOSE_COMMAND__"),
                                                ("name", "bash"),
                                            ),
                                        ),
                                        ((("name", "__finish__"),),),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        "dependencies": (),
        "timeout": 45,
        "role": "CONTROL",
    },
    "agent_api_close_retry_incomplete": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "agent_api_close_retry_incomplete"),),
        "execution_parameters": (
            (
                "clipboard_api_variants",
                (
                    (
                        ("heartbeat_file", "close_helper.heartbeat"),
                        ("inject_fault", True),
                        ("label", "target"),
                        ("lease_file", "close_helper.lease"),
                        ("operation", "close"),
                        ("pid_file", "close_helper.pid"),
                        ("prompt", "CLOSE_ROUTE_6501"),
                        ("state_file", "close_helper.state"),
                        (
                            "stub",
                            (
                                ("mode", "seq_probe"),
                                ("probe_tokens", ("CLOSE_ROUTE_6501",)),
                                (
                                    "rounds",
                                    (
                                        (
                                            (
                                                ("args", (("run_in_background", True),)),
                                                ("command", "__CLOSE_COMMAND__"),
                                                ("name", "bash"),
                                            ),
                                        ),
                                        ((("name", "__finish__"),),),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        "dependencies": (("agent_api_close_retry_incomplete_control", "GATE"),),
        "timeout": 45,
        "role": "TARGET",
    },
    "copy_utf8_clipboard_control": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "copy_utf8_clipboard_control"),),
        "execution_parameters": (
            ("kind", "control"),
            (
                "clipboard_api_variants",
                (
                    (
                        ("capture", "clipboard_capture.bin"),
                        ("label", "control"),
                        ("meta", "clipboard_meta.json"),
                        ("operation", "copy"),
                        ("prompt", "COPY_ROUTE_6601"),
                        (
                            "stub",
                            (
                                ("mode", "fixed_reply"),
                                ("probe_tokens", ("COPY_ROUTE_6601",)),
                                ("reply_content", "COPY_ASCII_6602 exact bytes"),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        "dependencies": (),
        "timeout": 45,
        "role": "CONTROL",
    },
    "copy_utf8_clipboard_bytes": {
        "commands": (),
        "seed_files": (),
        "config_keys": (),
        "observed_files": (),
        "profile": "STRUCTURED_FACTS",
        "oracle_parameters": (("fact_key", "copy_utf8_clipboard_bytes"),),
        "execution_parameters": (
            (
                "clipboard_api_variants",
                (
                    (
                        ("capture", "clipboard_capture.bin"),
                        ("label", "target"),
                        ("meta", "clipboard_meta.json"),
                        ("operation", "copy"),
                        ("prompt", "COPY_ROUTE_6601"),
                        (
                            "stub",
                            (
                                ("mode", "fixed_reply"),
                                ("probe_tokens", ("COPY_ROUTE_6601",)),
                                ("reply_content", "COPY_UTF8_6603 雪豹 café Δ"),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        "dependencies": (("copy_utf8_clipboard_control", "GATE"),),
        "timeout": 45,
        "role": "TARGET",
    },
    "custom_args_single_expansion_control": {
        "commands": ("/mode yolo", "/argguard ARGS_SAFE_7103", "continue the controlled custom command"),
        "seed_files": (
            (
                "commands/argguard.md",
                "ARGS_CONTROL_FACE_7101 {{args}} @{args_control_source.txt} !{printf 'ARGS_CONTROL_EXEC_7105\\n' > args_control_exec.txt}\n",
            ),
            ("args_control_source.txt", "ARGS_CONTROL_FILE_7104\n"),
        ),
        "config_keys": (),
        "observed_files": ("args_control_exec.txt",),
        "profile": "CUSTOM_SINGLE_EXPANSION",
        "oracle_parameters": (
            ("face_token", "ARGS_CONTROL_FACE_7101"),
            ("args_token", "ARGS_SAFE_7103"),
            ("file_token", "ARGS_CONTROL_FILE_7104"),
            ("path", "args_control_exec.txt"),
        ),
        "execution_parameters": (
            ("kind", "control"),
            (
                "stub",
                (("probe_tokens", ("ARGS_CONTROL_FACE_7101", "ARGS_SAFE_7103", "ARGS_CONTROL_FILE_7104")),),
            ),
        ),
        "dependencies": (),
        "timeout": 90,
        "role": "CONTROL",
    },
    "custom_args_no_secondary_expansion": {
        "commands": (
            "/mode yolo",
            "/argguard ARGS_SAFE_7103 @{args_secret.txt} !{printf 'ARGS_TARGET_EXEC_7107\\n' > args_target_exec.txt}",
            "continue the guarded custom command",
        ),
        "seed_files": (
            ("commands/argguard.md", "ARGS_TARGET_FACE_7102 {{args}}\n"),
            ("args_secret.txt", "ARGS_SECRET_READ_7106\n"),
        ),
        "config_keys": (),
        "observed_files": ("args_target_exec.txt",),
        "profile": "CUSTOM_NO_SECONDARY",
        "oracle_parameters": (
            ("face_token", "ARGS_TARGET_FACE_7102"),
            ("args_token", "ARGS_SAFE_7103"),
            ("secret_token", "ARGS_SECRET_READ_7106"),
            ("path", "args_target_exec.txt"),
        ),
        "execution_parameters": (
            (
                "stub",
                (("probe_tokens", ("ARGS_TARGET_FACE_7102", "ARGS_SAFE_7103", "ARGS_SECRET_READ_7106")),),
            ),
        ),
        "dependencies": (("custom_args_single_expansion_control", "GATE"),),
        "timeout": 90,
        "role": "TARGET",
    },
}
_ROWS = {**_ROWS, **_PLUGIN_ROWS, **_CHILD_STATE_ROWS}


def _home_seed_files(row: dict) -> tuple[tuple[str, str], ...]:
    """Lift a row's ``home_seed`` declaration into the typed recipe field.

    Rows carry these as an execution parameter; the fixture writes only the
    recipe field, and the extension scenarios that turn on a user-level
    definition need it on disk.  Reading the existing declaration keeps one
    statement of the fixture rather than a second copy free to drift from it.
    """
    declared = dict(row["execution_parameters"]).get("home_seed", ())
    return tuple(
        (
            (str(path), str(content))
            for path, content in declared
            if isinstance(path, str) and isinstance(content, str)
        )
    )


SCENARIOS = tuple(
    (
        define_state_scenario(
            test_id,
            StateRecipe(
                commands=row["commands"],
                seed_files=row["seed_files"],
                home_seed_files=_home_seed_files(row),
                config_keys=row["config_keys"],
                observed_files=row["observed_files"],
                oracle_profile=StateOracleProfile[row["profile"]],
                oracle_parameters=row["oracle_parameters"],
                execution_parameters=row["execution_parameters"],
                dependencies=tuple(
                    ((test_id, DependencyKind[kind]) for test_id, kind in row["dependencies"])
                ),
                timeout_seconds=row["timeout"],
            ),
            ScenarioRole[row["role"]],
        )
        for test_id, row in _ROWS.items()
    )
)
_BY_ID = {scenario.definition.scenario_id: scenario for scenario in SCENARIOS}


def scenario_by_id(scenario_id: str):
    try:
        return _BY_ID[scenario_id]
    except KeyError as error:
        raise KeyError(f"unknown state child scenario: {scenario_id}") from error


__all__ = ["SCENARIOS", "scenario_by_id"]
