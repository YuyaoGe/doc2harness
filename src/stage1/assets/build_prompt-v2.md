# Build task

The working directory `/opt/f0` contains an empty project: package layout, entry
point shells, and interface signatures are present, but the function bodies are
unimplemented. Read `.d2a_spec/functional_spec.md` (the functional
specification) and `.d2a_spec/AGENT_CONTRACT.md` (the entry-point contract),
then build the coding agent those documents describe, in place, under `/opt/f0`.

## Requirements

1. **Modify the sources in `/opt/f0` in place.** Do not create a separate
   project and do not write code elsewhere.
2. **Implement every capability in `.d2a_spec/functional_spec.md`**: the built-in
   tools, top-level commands, permission and role modes, global flags, in-session
   slash commands and keyboard shortcuts, the persistent memory system, the
   convention files, and the exit codes.
3. You are free to restructure the architecture and to add improvements of your
   own — context management, error handling, prompt engineering, and so on.

## Deliverables

See `.d2a_spec/functional_spec.md` §0.1. All of the following are required:

- **`/opt/f0/install.sh`** — a single-command install, after which the `yuyao`
  command is available. Any new third-party dependency must be declared in
  `[project.dependencies]` in `pyproject.toml` (or in `requirements.txt`); the
  runtime environment is offline, so an undeclared dependency fails at import.
- **An interactive TUI** — `yuyao` starts an interactive session supporting the
  slash commands and shortcuts in §5 of the specification.
- **A headless form, `yuyao -p "<task>"` (`--print`)** — does not enter the
  REPL, runs the task to termination, writes the result to stdout, exits with the
  codes defined in §9, and accepts the global flags in §4.
- **`/opt/f0/run_agent.sh`** — drives the headless entry point `entry.py`
  unattended, through the interface defined by `.d2a_spec/AGENT_CONTRACT.md` and
  §0.1.1 of the specification.

  Note that `run_agent.sh` and `entry.py` must be self-contained and runnable as
  shipped. The directory is copied verbatim into a clean container and invoked
  directly with `bash run_agent.sh …`: **`install.sh` is not run first, no `pip
  install` occurs, and `PYTHONPATH` is empty.** `entry.py` must therefore put its
  own source root on `sys.path` itself — for example
  `sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))` before
  importing your package — and must not assume the package is installed or that
  `PYTHONPATH` is set externally. Verify this in a fresh shell with nothing
  installed and `PYTHONPATH` unset.

- **`yuyao.agent_api`** — see §0.1.2 of the specification.

## Model access

Model connectivity is supplied through the environment variables
`D2A_MODEL_BASE_URL`, `D2A_MODEL_NAME`, and `D2A_MODEL_API_KEY` (see §0.2).
Calls must go through that endpoint only; no other address or credential may be
hardcoded. Cost accounting rates are injected through `D2A_MODEL_COST`.

`.d2a_spec/model_call_reference.py` shows the calling convention and can be
reused directly. Before building, running `python .d2a_spec/model_call_reference.py`
performs a round-trip smoke test to confirm connectivity.

When those variables are already present in the environment, the TUI must read
them at startup and connect without further configuration.

## Naming

The product is named `yuyao agent` and its command is `yuyao`. No user-visible
surface may mention `mini` or carry a non-English product name — this includes
the startup banner, window titles, help output, the prompt, the version string,
and configuration and history file paths. Internal module names may retain their
internal package name. The kernel's own startup banner must be suppressed (for
example by setting `MSWEA_SILENT_STARTUP=1`). For the README and architecture
document, see §0.5.

## Submission

Before submitting, perform a real install and a real run in a clean environment:
`install.sh` completes, `yuyao` enters an interactive session and completes one
task against the live model, and `run_agent.sh` solves a small task unattended in
a toy repository. Perform the headless run in a clean shell — for example
`env -u PYTHONPATH bash run_agent.sh …`.

> You have {{MAX_SUBMISSIONS}} submissions in total. Each `touch .build-submit`
> consumes one. When they are exhausted, the build ends with whatever the
> artifact is at that moment.

1. Confirm that `/opt/f0/run_agent.sh` exists, is executable, and is
   syntactically valid under `bash -n`.
2. Run `touch /opt/f0/.build-submit`.
3. The system validates the artifact and writes the result to
   `/opt/f0/.build-result` as JSON: `contract_ok`, plus a `hint` describing what
   to address when validation fails. While validation is in progress the marker
   `/opt/f0/.build-checking` is present; **as long as it exists the check is
   still running, so do not `touch .build-submit` again.** Poll `.build-result`
   every 20–30 seconds, and continue improving the code while you wait.
4. If `contract_ok=false`, address the `hint`, then `rm /opt/f0/.build-result`
   and `touch /opt/f0/.build-submit` again.
5. `contract_ok=true` ends the build.
