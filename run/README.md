# Run commands

`run_from_config.sh` is the generic YAML entry point:

```sh
./run/run_from_config.sh run/configs/project.yaml --dry-run
```

Install the small YAML-only run dependency when setting up a fresh environment:

```sh
python3 -m pip install -r requirements/run.txt
```

The YAML declares the catalog selection, models, output directory, and an
explicit production executor command.  The command is executed once per model
with `SBH_MODEL_ID`, `SBH_RUN_CONFIG`, `SBH_SCENARIO_COUNT`, and the validated
`SBH_IDS_FILE` selection in its environment.  There is no built-in fallback
executor, and an existing receipt is never re-read as if it were a fresh score.

The two-candidate command is deliberately strict:

```sh
export CANDIDATE_A_ARTIFACT=/path/to/first/artifact
export CANDIDATE_B_ARTIFACT=/path/to/second/artifact
./run/run_94_example.sh --dry-run
```

It requires exactly the complete active catalog (currently 94 IDs) and the
two model IDs `candidate-a` and `candidate-b`.  A real run uses the
`run_production_executor.py` adapter and requires both artifact directories.
Missing or unresolved paths fail closed instead of claiming that the 94-point
run completed.  The executor drives each scenario directly; it never reads old
result rows or verdicts.
