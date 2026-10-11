"""Typed public boundary for interactive TUI PTY scenarios."""

from .adapter import (
    TuiPtyExecutionPlan,
    build_tui_pty_execution_plan,
)
from .contracts import (
    HistoryEvent,
    HistoryFile,
    HistoryObservation,
    HistorySnapshot,
    ObservedFile,
    PromptObservation,
    TuiApplicability,
    TuiEvaluation,
    TuiInput,
    TuiObservation,
    TuiOracleBinding,
    TuiOutcome,
    TuiProfile,
    TuiRecipe,
    TuiRequest,
    TuiScenario,
)
from .recipes import SCENARIO_BY_ID

__all__ = [
    "SCENARIO_BY_ID",
    "HistoryEvent",
    "HistoryFile",
    "HistoryObservation",
    "HistorySnapshot",
    "ObservedFile",
    "PromptObservation",
    "TuiApplicability",
    "TuiEvaluation",
    "TuiInput",
    "TuiObservation",
    "TuiOracleBinding",
    "TuiOutcome",
    "TuiProfile",
    "TuiPtyExecutionPlan",
    "TuiRecipe",
    "TuiRequest",
    "TuiScenario",
    "build_tui_pty_execution_plan",
]
