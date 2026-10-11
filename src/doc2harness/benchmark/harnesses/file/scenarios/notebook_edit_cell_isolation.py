"""Verify notebook editing changes only the selected cell.
1. Seed three cells, enable yolo mode, and read the notebook beside a live Bash probe.
2. Replace cell 1 through the explicit notebook-edit alias pair.
3. Dump the notebook in a later Bash round and capture its typed notebook fact.
4. Pass when cell 1 is new while cells 0 and 2 retain their exact JSON structure and execution is clean.
"""

import json
from dataclasses import replace

from doc2harness.benchmark.harnesses.file.contracts import (
    FileToolApplicability,
    FileToolJudgement,
    FileToolOracleProfile,
    FileToolOutcome,
    FileToolRawObservation,
    FileToolScenario,
    build_file_tool_evaluation,
    capture_fact,
    compile_file_tool_steps,
    setup_command,
    tool_round,
    validate_file_tool_observation,
    write_fixture,
)
from ..recipes import scenario_by_id

SCENARIO_ID = "notebook_edit_cell_isolation"
STEPS = (
    write_fixture(
        "nb.ipynb",
        '{"cells": [{"cell_type": "markdown", "metadata": {"tags": '
        '["KEEP0"], "nested": {"k": 0}}, "source": ["CELL0_KEEP\\n"]}, '
        '{"cell_type": "code", "metadata": {"target": true}, "source": '
        '["CELL1_OLD\\n"], "outputs": [], "execution_count": null}, '
        '{"cell_type": "code", "metadata": {"tags": ["KEEP2"], "nested": '
        '{"k": 2}}, "source": ["CELL2_KEEP\\n"], "outputs": '
        '[{"output_type": "stream", "name": "stdout", "text": '
        '["KEEP2_OUTPUT\\n"]}], "execution_count": 42}], "metadata": {}, '
        '"nbformat": 4, "nbformat_minor": 5}',
    ),
    setup_command("/mode yolo"),
    tool_round("bash", turn=1, command="echo PERM_LIVE_7755 > live.txt"),
    tool_round("read", turn=1, arguments_nested=True, file_path="nb.ipynb"),
    tool_round(
        ("notebook_edit", "notebook-edit"),
        turn=2,
        aliases_explicit=True,
        arguments_nested=True,
        file_path="nb.ipynb",
        cell_index=1,
        mode="replace",
        source="CELL1_NEW",
    ),
    tool_round("bash", turn=3, command="cat nb.ipynb"),
    capture_fact("nb", required=(), min_requests=2),
)
_BASE_SCENARIO = scenario_by_id(SCENARIO_ID)
_COMPILED_RECIPE = compile_file_tool_steps(
    STEPS,
    case_kind="reject",
    declared_tools=("notebook_edit",),
    oracle_profile=FileToolOracleProfile.NOTEBOOK_ISOLATED,
    blocking=True,
    execution_parameters=_BASE_SCENARIO.recipe.execution_parameters,
    bind_oracle_parameters=True,
)
RECIPE = replace(
    _COMPILED_RECIPE,
    setup_commands=_BASE_SCENARIO.recipe.setup_commands,
    seed_files=_BASE_SCENARIO.recipe.seed_files,
    rounds=_BASE_SCENARIO.recipe.rounds,
    oracle_parameters=_BASE_SCENARIO.recipe.oracle_parameters,
)
SCENARIO: FileToolScenario = FileToolScenario(_BASE_SCENARIO.definition, RECIPE)
scenario = SCENARIO
definition = SCENARIO.definition


def judge(observation: FileToolRawObservation):
    """Apply the typed notebook-isolation contract."""

    validate_file_tool_observation(SCENARIO, observation)
    advertised = any(
        type(item) is str
        and item.strip().lower().replace("-", "_") == "notebook_edit"
        for item in observation.advertised_tools
    )
    if not advertised and not observation.timed_out:
        return build_file_tool_evaluation(
            SCENARIO,
            observation,
            FileToolJudgement(
                FileToolOutcome.PASS,
                FileToolApplicability.INCONCLUSIVE,
                FileToolOracleProfile.NOTEBOOK_ISOLATED.value,
            ),
        )
    params = dict(RECIPE.oracle_parameters)
    raw = dict(observation.facts).get("nb")
    try:
        notebook = json.loads(raw) if type(raw) is str else None
    except (json.JSONDecodeError, TypeError, ValueError):
        notebook = None
    cells = notebook.get("cells") if isinstance(notebook, dict) else None
    shape = isinstance(cells, list) and len(cells) == 3
    blob = json.dumps(cells, ensure_ascii=False) if shape else ""
    cell0 = (
        json.dumps(cells[0], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if shape
        else ""
    )
    cell2 = (
        json.dumps(cells[2], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if shape
        else ""
    )
    passed = (
        advertised
        and observation.request_count >= int(params["min_requests"])
        and observation.live
        and shape
        and str(params["new_marker"]) in blob
        and str(params["old_marker"]) not in blob
        and cell0 == params["expected_cell0_json"]
        and cell2 == params["expected_cell2_json"]
        and observation.process_tree_clean
        and observation.gateway_clean
        and observation.workspace_clean
        and not observation.timed_out
    )
    return build_file_tool_evaluation(
        SCENARIO,
        observation,
        FileToolJudgement(
            FileToolOutcome.PASS if passed else FileToolOutcome.FAIL,
            FileToolApplicability.APPLICABLE,
            FileToolOracleProfile.NOTEBOOK_ISOLATED.value
            if passed
            else f"{FileToolOracleProfile.NOTEBOOK_ISOLATED.value}_FAILED",
        ),
    )


__all__ = ["RECIPE", "SCENARIO", "STEPS", "definition", "judge", "scenario"]
