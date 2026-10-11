# Entry-point contract

The format of the two input files `run_agent.sh` receives. For the remaining
interface requirements, see `functional_spec.md` §0.1.1.

## `task.json` (referenced by `--task-file`, read-only)

```json
{
  "task_id": "<id>",
  "type": "<type name>",
  "problem_statement": "<problem text>",
  "repo_dir": "/app",
  "allow_network": false
}
```

- `problem_statement` — the problem to solve, as plain text.
- `repo_dir` — path to the repository to modify; identical to `--repo-dir`.
- `allow_network` — whether network access is permitted for this run.

## `budget.json` (referenced by `--budget-file`, read-only)

```json
{ "max_steps": 50, "max_tokens": 0, "max_wall_seconds": 1800 }
```

- `max_steps` — maximum number of steps, where one step is approximately one
  model call. On reaching the limit the agent must stop cleanly and record
  `LimitsExceeded`.
- `max_tokens` — `0` means no limit is enforced on the agent side.
- `max_wall_seconds` — wall-clock limit, in seconds.
