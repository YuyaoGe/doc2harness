"""Frozen typed recipes for interactive TUI PTY scenarios."""

from doc2harness.benchmark.definition import DependencyKind, ScenarioRole
from .contracts import HistoryScenario, TuiInput, TuiOracleBinding, TuiProfile, TuiRecipe, define_tui_scenario


def _inputs(*values: str) -> tuple[TuiInput, ...]:
    return tuple((TuiInput(value) for value in values))


_HISTORY_A = "H8A9961_ONLY_A_4C7E"
_HISTORY_B = "H8B9962_ONLY_B_9D3F"
_ROWS: dict[str, tuple[TuiRecipe, ScenarioRole]] = {
    "tui_ctrl_byte_storm_survives": (
        TuiRecipe(
            TuiProfile.CHAOS_SURVIVES,
            "sha256:ebf1873599579122839f864b8a08fb9fc6638611524bd5f2470bbfbac377a1a1",
            (),
            90,
            False,
            _inputs("010205060708090b0c0e0f101112131415161718192052423937415f37373331"),
            probe_tokens=("RB97A_7731",),
        ),
        ScenarioRole.TARGET,
    ),
    "tui_bang_alone_enters_shell_mode": (
        TuiRecipe(
            TuiProfile.FILE_WRITTEN,
            "sha256:863a252325195b0e72491af1418666147ad20a9281fbb0af834816eb546d85fd",
            (),
            100,
            False,
            _inputs("21", "7072696e746620274d4f44453130305f25735c6e272037373331203e206d6f64653130302e747874"),
            probe_tokens=("MODE100_7731",),
            observed_files=("mode100.txt",),
            oracle=TuiOracleBinding(primary_token="MODE100_7731", path="mode100.txt"),
        ),
        ScenarioRole.TARGET,
    ),
    "history_workspace_a_fresh_control": (
        TuiRecipe(
            TuiProfile.HISTORY_FRESH,
            "sha256:5491f4c5e2e792ee9471ed98fc1b679b860508c0515b68c2d0dec1dd5b219927",
            (),
            90,
            False,
            gateway_mode="fixed_reply",
            reply_content="history-control-a-ok",
            probe_tokens=(_HISTORY_A, _HISTORY_B),
            history_scenario=HistoryScenario.FRESH_A,
            oracle=TuiOracleBinding(
                primary_token=_HISTORY_A, foreign_token=_HISTORY_B, history_prefix="H8A9961_"
            ),
        ),
        ScenarioRole.CONTROL,
    ),
    "history_workspace_b_fresh_control": (
        TuiRecipe(
            TuiProfile.HISTORY_FRESH,
            "sha256:8f4b916ebf70fc02b2ddd923d763297f35633e263bfb249b971f22e38e67a8ed",
            (),
            90,
            False,
            gateway_mode="fixed_reply",
            reply_content="history-control-b-ok",
            probe_tokens=(_HISTORY_A, _HISTORY_B),
            history_scenario=HistoryScenario.FRESH_B,
            oracle=TuiOracleBinding(
                primary_token=_HISTORY_B, foreign_token=_HISTORY_A, history_prefix="H8B9962_"
            ),
        ),
        ScenarioRole.CONTROL,
    ),
    "history_realpath_key": (
        TuiRecipe(
            TuiProfile.HISTORY_REALPATH,
            "sha256:b6c88c978f949efceef60880ebe1e8ff1f1a7e74d4c65b8a648f1738fc14137b",
            (
                ("history_workspace_a_fresh_control", DependencyKind.GATE),
                ("history_workspace_b_fresh_control", DependencyKind.GATE),
            ),
            140,
            True,
            gateway_mode="fixed_reply",
            reply_content="history-realpath-ok",
            probe_tokens=(_HISTORY_A, _HISTORY_B),
            history_scenario=HistoryScenario.REALPATH_KEY,
            oracle=TuiOracleBinding(primary_token=_HISTORY_A, secondary_token=_HISTORY_B),
        ),
        ScenarioRole.TARGET,
    ),
}
SCENARIO_BY_ID = {
    scenario_id: define_tui_scenario(scenario_id, recipe, role)
    for scenario_id, (recipe, role) in _ROWS.items()
}


def scenario_by_id(scenario_id: str, /):
    return SCENARIO_BY_ID[scenario_id]
