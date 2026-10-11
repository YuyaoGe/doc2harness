"""Deterministic gateway stimulus for the TUI scenarios.

Stimulus construction belongs to the execution layer, so it ships with the
driver that needs it.
"""

from __future__ import annotations


def tui_gateway_config(scenario, gateway_module):
    """Build the deterministic gateway stimulus for one TUI scenario.

    The mock gateway has no standalone ``background`` or ``gpg_fail`` response
    mode.  Those names describe the scenario, not a wire response.  Translate
    them into explicit ``seq_probe`` rounds here so the artifact receives
    explicit tool calls; an empty sequence is never silently
    treated as a successful stimulus.
    """
    sid = scenario.definition.scenario_id
    marker = scenario.recipe.oracle.primary_token or "TUI_EXTERNAL_7731"
    cfg = gateway_module.new_cfg(
        mode=scenario.recipe.gateway_mode,
        reply_content=scenario.recipe.reply_content,
        probe_tokens=list(scenario.recipe.probe_tokens),
    )
    if "background_completion" in sid:
        cfg.update(
            mode="seq_probe",
            rounds=[
                [{
                    "name": "bash",
                    "command": (
                        "python3 -c \"from pathlib import Path; "
                        f"Path('background_done.marker').write_text('{marker} completed'); "
                        f"print('{marker} completed')\""
                    ),
                    "background": True,
                }],
                [{"name": "bash", "command": "sleep 1; echo background finished"}],
                [{"name": "__finish__"}],
            ],
        )
    elif "autotest_output" in sid:
        cfg.update(
            mode="seq_probe",
            rounds=[
                [{
                    "name": "write",
                    "args": {"path": "autotest_target.txt", "content": marker + "\\n"},
                }],
                [{"name": "__finish__"}],
            ],
        )
    elif "autocommit_fail" in sid:
        cfg.update(
            mode="seq_probe",
            rounds=[
                [{
                    "name": "write",
                    "args": {"path": "autocommit_target.txt", "content": marker + "\\n"},
                }],
                [{"name": "__finish__"}],
            ],
        )
    elif "step_limit" in sid:
        cfg.update(
            mode="seq_probe",
            rounds=[
                [{"name": "bash", "command": f"printf '{marker} max_steps\\n' >> step_limit.marker"}]
                for _ in range(10)
            ],
        )
    elif scenario.recipe.gateway_mode == "seq_probe":
        # Finish-reason has to contain one real tool-call round before close.
        cfg.update(
            rounds=[
                [{"name": "bash", "command": f"printf '{marker} tool_call\\n'"}],
                [{"name": "__finish__"}],
            ]
        )
    elif scenario.recipe.gateway_mode == "recover_bad_json":
        cfg.update(
            mode="recover_bad_json",
            command=f"printf '{marker} recovered\\n' | tee bad_args.marker",
        )
    if len(scenario.recipe.input_lines) > 1 and cfg.get("mode") == "normal":
        # A point that types more than one line needs each turn to *end*.  The
        # standing tool call of the plain mode is answered the same way every
        # round, so an unattended session repeats it until its step budget is
        # gone - and the later lines, which are what the point is about, are
        # typed into a session with nothing left to spend.  One tool call then
        # a plain reply bounds each turn without changing what any single-line
        # point observes.
        cfg.update(
            mode="seq_probe",
            rounds=[
                [{"name": "bash", "command": cfg.get("command") or "true"}],
                [{"name": "__finish__"}],
            ] * (len(scenario.recipe.input_lines) + 2),
        )
    return cfg


__all__ = ["tui_gateway_config"]
