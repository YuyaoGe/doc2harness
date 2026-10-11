"""Frozen recipes for child-process file-tool scenarios."""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole
from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolCall,
    FileToolOracleProfile,
    FileToolRecipe,
    define_file_tool_scenario,
)

_ROWS = {
    "glob_finds_inside_control": {
        "case_kind": "control",
        "declared_tools": ("glob",),
        "setup_commands": ("/mode yolo",),
        "seed_files": (("inside_ok.py", "x\n"),),
        "rounds": (
            (
                (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                (("glob",), (("pattern", "**/*.py"),), False, True),
                (("glob",), (("pattern", "*.py"),), False, True),
            ),
        ),
        "oracle_profile": "TEXT_RULES",
        "oracle_parameters": (
            ("source", "model_text"),
            ("required", ("inside_ok",)),
            ("forbidden", ()),
            ("min_requests", 2),
        ),
        "dependencies": (),
        "blocking": False,
        "role": "CONTROL",
        "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
        "bind_oracle_parameters": True,
    },
    "grep_finds_needle_control": {
        "case_kind": "control",
        "declared_tools": ("grep",),
        "setup_commands": ("/mode yolo",),
        "seed_files": (("plain_gi.txt", "NEEDLE_GI_7731 here\n"),),
        "rounds": (
            (
                (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                (("grep",), (("pattern", "NEEDLE_GI_7731"), ("path", ".")), False, True),
            ),
        ),
        "oracle_profile": "TEXT_RULES",
        "oracle_parameters": (
            ("source", "model_text"),
            ("required", ("plain_gi.txt",)),
            ("forbidden", ()),
            ("min_requests", 2),
        ),
        "dependencies": (),
        "blocking": False,
        "role": "CONTROL",
        "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
        "bind_oracle_parameters": True,
    },
    "grep_type_filter_ignored": {
        "case_kind": "reject",
        "declared_tools": ("grep",),
        "setup_commands": ("/mode yolo",),
        "seed_files": (
            ("gt_hit.py", "NEEDLE_GTYPE_7731 python\n"),
            ("gt_hit.txt", "NEEDLE_GTYPE_7731 text\n"),
        ),
        "rounds": (
            (
                (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                (("grep",), (("pattern", "NEEDLE_GTYPE_7731"), ("path", "."), ("type", "py")), False, True),
            ),
        ),
        "oracle_profile": "TEXT_RULES",
        "oracle_parameters": (
            ("source", "model_text"),
            ("required", ("gt_hit.py",)),
            ("forbidden", ("gt_hit.txt",)),
            ("min_requests", 2),
        ),
        "dependencies": (("grep_finds_needle_control", "GATE"),),
        "blocking": False,
        "role": "TARGET",
        "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
        "bind_oracle_parameters": True,
    },
    "undo_reverts_overwrite_control": {
        "case_kind": "control",
        "declared_tools": ("write",),
        "setup_commands": ("/mode yolo",),
        "seed_files": (("tgt.txt", "ORIG_PERM_0001\n"),),
        "rounds": (),
        "oracle_profile": "TRANSITION_EXACT",
        "oracle_parameters": (
            (
                "expected",
                (
                    ("phase1_tgt", "OVR_BODY_7755\n"),
                    ("after_mid1_tgt", "ORIG_PERM_0001\n"),
                    ("tgt", "ORIG_PERM_0001\n"),
                ),
            ),
            ("min_requests", 2),
        ),
        "dependencies": (),
        "blocking": False,
        "role": "CONTROL",
        "execution_parameters": (
            (
                "phase1_rounds",
                (
                    (
                        (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                        (("read",), (("file_path", "tgt.txt"),), False, True),
                    ),
                    ((("write",), (("file_path", "tgt.txt"), ("content", "OVR_BODY_7755\n")), False, True),),
                ),
            ),
            ("mid_slash", ("/undo",)),
            ("phase2_rounds", (((("bash",), (("command", "echo POSTUNDO_7755"),), False, False),),)),
            ("deny", ()),
            ("allow", "echo PERM_LIVE_7755 > live.txt"),
        ),
        "bind_oracle_parameters": True,
    },
    "rewind_restores_edited_file": {
        "case_kind": "reject",
        "declared_tools": ("write",),
        "setup_commands": ("/mode yolo",),
        "seed_files": (("tgt.txt", "ORIG_PERM_0001\n"),),
        "rounds": (),
        "oracle_profile": "REWIND_RESTORES",
        "oracle_parameters": (("expected", "ORIG_PERM_0001\n"), ("min_requests", 2)),
        "dependencies": (("undo_reverts_overwrite_control", "GATE"),),
        "blocking": False,
        "role": "TARGET",
        "execution_parameters": (
            (
                "phase1_rounds",
                (
                    (
                        (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                        (("read",), (("file_path", "tgt.txt"),), False, True),
                    ),
                    ((("write",), (("file_path", "tgt.txt"), ("content", "RWOVR_8821\n")), False, True),),
                ),
            ),
            ("mid_slash", ("/rewind 1", "/rewind 0", "/rewind")),
            ("phase2_rounds", (((("bash",), (("command", "echo POSTRW_8821"),), False, False),),)),
            ("deny", ()),
            ("allow", "echo PERM_LIVE_7755 > live.txt"),
        ),
        "bind_oracle_parameters": True,
    },
}
_ROWS.update(
    {
        "notebook_edit_cell_isolation": {
            "case_kind": "reject",
            "declared_tools": ("notebook_edit",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (
                (
                    "nb.ipynb",
                    '{"cells": [{"cell_type": "markdown", "metadata": {"tags": ["KEEP0"], "nested": {"k": 0}}, "source": ["CELL0_KEEP\\n"]}, {"cell_type": "code", "metadata": {"target": true}, "source": ["CELL1_OLD\\n"], "outputs": [], "execution_count": null}, {"cell_type": "code", "metadata": {"tags": ["KEEP2"], "nested": {"k": 2}}, "source": ["CELL2_KEEP\\n"], "outputs": [{"output_type": "stream", "name": "stdout", "text": ["KEEP2_OUTPUT\\n"]}], "execution_count": 42}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}',
                ),
            ),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (("read",), (("file_path", "nb.ipynb"),), False, True),
                ),
                (
                    (
                        ("notebook_edit", "notebook-edit"),
                        (
                            ("file_path", "nb.ipynb"),
                            ("cell_index", 1),
                            ("mode", "replace"),
                            ("source", "CELL1_NEW"),
                        ),
                        True,
                        True,
                    ),
                ),
                ((("bash",), (("command", "cat nb.ipynb"),), False, False),),
            ),
            "oracle_profile": "NOTEBOOK_ISOLATED",
            "oracle_parameters": (
                (
                    "expected_cell0_json",
                    '{"cell_type":"markdown","metadata":{"nested":{"k":0},"tags":["KEEP0"]},"source":["CELL0_KEEP\\n"]}',
                ),
                (
                    "expected_cell2_json",
                    '{"cell_type":"code","execution_count":42,"metadata":{"nested":{"k":2},"tags":["KEEP2"]},"outputs":[{"name":"stdout","output_type":"stream","text":["KEEP2_OUTPUT\\n"]}],"source":["CELL2_KEEP\\n"]}',
                ),
                ("new_marker", "CELL1_NEW"),
                ("old_marker", "CELL1_OLD"),
                ("min_requests", 2),
            ),
            "dependencies": (),
            "blocking": True,
            "role": "TARGET",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "glob_truncation_keeps_sorted_end": {
            "case_kind": "reject",
            "declared_tools": ("glob",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (
                        ("bash",),
                        (
                            (
                                "command",
                                "mkdir -p gord && python3 -c \"import pathlib;pathlib.Path('gord/OLD' + 'EST_7755.ordx').write_text('x')\" && touch -t 200001010000 gord/OLD*_7755.ordx && python3 -c \"import os\nfor i in range(800): open('gord/n%d.ordx' % i,'w').write('x')\" && echo ORD_SEEDED_7755",
                            ),
                        ),
                        False,
                        False,
                    ),
                ),
                ((("glob",), (("pattern", "**/*.ordx"),), False, True),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", (".ordx",)),
                ("forbidden", ("OLDEST_7755",)),
                ("min_requests", 2),
            ),
            "dependencies": (("glob_finds_inside_control", "GATE"),),
            "blocking": True,
            "role": "TARGET",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
    }
)
_ROWS.update(
    {
        "notebook_edit_delete_keeps_neighbors": {
            "case_kind": "reject",
            "declared_tools": ("notebook_edit",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (
                (
                    "nb.ipynb",
                    '{"cells": [{"cell_type": "markdown", "metadata": {"tags": ["KEEP0"], "nested": {"k": 0}}, "source": ["CELL0_KEEP\\n"]}, {"cell_type": "code", "metadata": {"target": true}, "source": ["CELL1_OLD\\n"], "outputs": [], "execution_count": null}, {"cell_type": "code", "metadata": {"tags": ["KEEP2"], "nested": {"k": 2}}, "source": ["CELL2_KEEP\\n"], "outputs": [{"output_type": "stream", "name": "stdout", "text": ["KEEP2_OUTPUT\\n"]}], "execution_count": 42}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}',
                ),
            ),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (("read",), (("file_path", "nb.ipynb"),), False, True),
                ),
                (
                    (
                        ("notebook_edit", "notebook-edit"),
                        (("file_path", "nb.ipynb"), ("cell_index", 1), ("mode", "delete")),
                        True,
                        True,
                    ),
                ),
                (
                    (
                        ("bash",),
                        (
                            (
                                "command",
                                "python3 -c \"import json;print('NBCELLS' + '_' + str(len(json.load(open('nb.ipynb'))['cells'])))\" && echo NBDEL_DONE_7755",
                            ),
                        ),
                        False,
                        False,
                    ),
                ),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("NBCELLS_2",)),
                ("forbidden", ()),
                ("min_requests", 2),
            ),
            "dependencies": (("notebook_edit_cell_isolation", "REQUIREMENT"),),
            "blocking": True,
            "role": "TARGET",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "symlink_to_outside_target_readable_control": {
            "case_kind": "control",
            "declared_tools": ("bash",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (
                        ("bash",),
                        (
                            (
                                "command",
                                "printf 'OUTSIDE_ORIG_7731\\n' > /tmp/outside_9931.txt && ln -sf /tmp/outside_9931.txt link_9931.txt && echo SYMCTL_DONE_7755",
                            ),
                        ),
                        False,
                        False,
                    ),
                ),
                (
                    (
                        ("bash",),
                        (
                            (
                                "command",
                                "python3 -c \"import os; p='link_9931.txt'; print('SYM'+'LINK_%d' % os.path.islink(p)); print('SYM'+'REAL_%d' % (os.path.realpath(p)=='/tmp/outside_9931.txt')); print('SYM'+'BYTE_%d' % (open(p,'rb').read()==b'OUTSIDE_ORIG_7731\\n'))\"",
                            ),
                        ),
                        False,
                        False,
                    ),
                ),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("SYMLINK_1", "SYMREAL_1", "SYMBYTE_1")),
                ("forbidden", ()),
                ("min_requests", 2),
            ),
            "dependencies": (),
            "blocking": True,
            "role": "CONTROL",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "p32_ctl_memory_get_reads_inside_workspace": {
            "case_kind": "control",
            "declared_tools": ("memory_get",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (("MEMORY.md", "INSIDEMEM_7732\n"),),
            "rounds": (),
            "oracle_profile": "MEMORY_EVENT",
            "oracle_parameters": (("marker", "INSIDEMEM_7732"), ("min_requests", 2)),
            "dependencies": (),
            "blocking": True,
            "role": "CONTROL",
            "execution_parameters": (
                (
                    "phase1_rounds",
                    (((("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),),),
                ),
                (
                    "phase2_rounds",
                    (
                        ((("memory_get", "memory-get"), (("path", "MEMORY.md"),), True, True),),
                        ((("bash",), (("command", "echo P32A_DONE_7755"),), False, False),),
                    ),
                ),
                ("mid_slash", ("/help",)),
                ("deny", ()),
                ("allow", "echo PERM_LIVE_7755 > live.txt"),
            ),
            "bind_oracle_parameters": True,
        },
        "memory_get_abs_path_escapes_workspace": {
            "case_kind": "reject",
            "declared_tools": ("memory_get",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (
                        ("bash",),
                        (
                            (
                                "command",
                                "python3 -c \"open('/tmp/msec_9932.txt','w').write('MEMSECRET' + '_7732' + chr(10))\" && echo P32A_SEEDED_7755",
                            ),
                        ),
                        False,
                        False,
                    ),
                ),
                ((("memory_get", "memory-get"), (("path", "/tmp/msec_9932.txt"),), True, True),),
                ((("bash",), (("command", "echo P32A_DONE_7755"),), False, False),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("P32A_DONE_7755",)),
                ("forbidden", ("MEMSECRET_7732",)),
                ("min_requests", 2),
            ),
            "dependencies": (("p32_ctl_memory_get_reads_inside_workspace", "GATE"),),
            "blocking": True,
            "role": "TARGET",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "read_via_symlink_outside_workspace_bounded": {
            "case_kind": "reject",
            "declared_tools": ("read",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (
                        ("bash",),
                        (
                            (
                                "command",
                                "python3 -c \"open('/tmp/secr_9931.txt','w').write('OUTSECRET' + '_7731' + chr(10))\" && ln -sf /tmp/secr_9931.txt ls_9931.txt && echo RS6_SEEDED_7755",
                            ),
                        ),
                        False,
                        False,
                    ),
                ),
                ((("read",), (("file_path", "ls_9931.txt"),), False, True),),
                ((("bash",), (("command", "echo RS6_DONE_7755"),), False, False),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("RS6_DONE_7755",)),
                ("forbidden", ("OUTSECRET_7731",)),
                ("min_requests", 2),
            ),
            "dependencies": (
                ("symlink_to_outside_target_readable_control", "GATE"),
                ("file_symlink_inside_still_readable_control", "GATE"),
            ),
            "blocking": True,
            "role": "TARGET",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "file_symlink_inside_still_readable_control": {
            "case_kind": "control",
            "declared_tools": ("read",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (("rif_9931.txt", "READ_INSIDE_7731\n"),),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (
                        ("bash",),
                        (("command", "ln -sf rif_9931.txt ril_9931.txt && echo RIF_SEEDED_7755"),),
                        False,
                        False,
                    ),
                ),
                ((("read",), (("file_path", "ril_9931.txt"),), False, True),),
                ((("bash",), (("command", "echo RIF_DONE_7755"),), False, False),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("READ_INSIDE_7731",)),
                ("forbidden", ()),
                ("min_requests", 2),
            ),
            "dependencies": (),
            "blocking": True,
            "role": "CONTROL",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "grep_default_mode_returns_path_control": {
            "case_kind": "control",
            "declared_tools": ("grep",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (("f9_9931.txt", "F9NEEDLE_7755 ctxf9_9931\n"),),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (
                        ("grep",),
                        (("pattern", "F9NEEDLE_7755"), ("path", "."), ("output_mode", "files_with_matches")),
                        False,
                        True,
                    ),
                ),
                ((("bash",), (("command", "echo F9C_DONE_7755"),), False, False),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "target_tool_result"),
                ("required", ("f9_9931.txt",)),
                ("forbidden", ("ctxf9_9931",)),
                ("forbidden_regex", "(?:^|\\n)\\s*[^:\\n]+:\\s*\\d+\\s*(?:\\n|$)"),
                ("min_requests", 2),
            ),
            "dependencies": (),
            "blocking": True,
            "role": "CONTROL",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "grep_head_limit_zero_not_unlimited": {
            "case_kind": "reject",
            "declared_tools": ("grep",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (("f9_9931.txt", "F9NEEDLE_7755 ctxf9_9931\n"),),
            "rounds": (
                (
                    (("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),
                    (
                        ("grep",),
                        (("pattern", "F9NEEDLE_7755"), ("path", "."), ("head_limit", 0)),
                        False,
                        True,
                    ),
                ),
                ((("bash",), (("command", "echo HZ9_DONE_7755"),), False, False),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("HZ9_DONE_7755",)),
                ("forbidden", ("f9_9931",)),
                ("min_requests", 2),
            ),
            "dependencies": (("grep_default_mode_returns_path_control", "GATE"),),
            "blocking": True,
            "role": "TARGET",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "read_regular_file_control": {
            "case_kind": "control",
            "declared_tools": ("read",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (("reg_9941.txt", "REGULAR_7731\n"),),
            "rounds": (
                ((("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),),
                ((("read",), (("file_path", "reg_9941.txt"),), False, True),),
                ((("bash",), (("command", "python3 -c \"print('DEVREAD' + '_DONE_7755')\""),), False, False),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("REGULAR_7731",)),
                ("forbidden", ()),
                ("min_requests", 2),
            ),
            "dependencies": (),
            "blocking": True,
            "role": "CONTROL",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
        "read_char_device_bounded": {
            "case_kind": "reject",
            "declared_tools": ("read",),
            "setup_commands": ("/mode yolo",),
            "seed_files": (),
            "rounds": (
                ((("bash",), (("command", "echo PERM_LIVE_7755 > live.txt"),), False, False),),
                ((("read",), (("file_path", "/dev/urandom"),), False, True),),
                ((("bash",), (("command", "python3 -c \"print('DEVREAD' + '_DONE_7755')\""),), False, False),),
            ),
            "oracle_profile": "TEXT_RULES",
            "oracle_parameters": (
                ("source", "model_text"),
                ("required", ("DEVREAD_DONE_7755",)),
                ("forbidden", ()),
                ("min_requests", 2),
            ),
            "dependencies": (("read_regular_file_control", "GATE"),),
            "blocking": True,
            "role": "TARGET",
            "execution_parameters": (("deny", ()), ("allow", "echo PERM_LIVE_7755 > live.txt")),
            "bind_oracle_parameters": True,
        },
    }
)


def _build(scenario_id: str):
    row = _ROWS[scenario_id]
    rounds = tuple(
        (
            tuple((FileToolCall(names, args, aliases, nested) for names, args, aliases, nested in turn))
            for turn in row["rounds"]
        )
    )
    recipe = FileToolRecipe(
        row["case_kind"],
        row["declared_tools"],
        row["setup_commands"],
        row["seed_files"],
        rounds,
        FileToolOracleProfile(row["oracle_profile"]),
        row["oracle_parameters"],
        tuple(((item, DependencyKind(kind)) for item, kind in row["dependencies"])),
        row["blocking"],
        execution_parameters=row.get("execution_parameters", ()),
        bind_oracle_parameters=row.get("bind_oracle_parameters", False),
    )
    return define_file_tool_scenario(scenario_id, recipe, ScenarioRole(row["role"]))


_SCENARIOS = {item: _build(item) for item in _ROWS}


def scenario_ids() -> tuple[str, ...]:
    return tuple(_SCENARIOS)


def scenario_by_id(scenario_id: str):
    try:
        return _SCENARIOS[scenario_id]
    except KeyError as error:
        raise KeyError(f"unknown file-tool-child scenario: {scenario_id}") from error


__all__ = ["scenario_by_id", "scenario_ids"]
