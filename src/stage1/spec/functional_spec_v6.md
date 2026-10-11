# Functional Specification — Super Coding Agent

> This document defines the complete external surface of a "super coding agent": built-in tools, top-level commands, global flags, permission and role modes, in-session slash commands, the persistent memory system, convention files, and exit codes.
> From this specification the builder must produce an agent that is installable, interactive, and capable of unattended execution.
> Every feature listed in this document is a required item.
> §8 adds cross-cutting consistency rules and specifies the externally observable surfaces on which the effect of each feature must land.
---

## 0. Deliverables and Runtime Interfaces

### 0.1 Deliverables
| Deliverable | Description |
|---|---|
| Installation script | One-shot installation (e.g. `install.sh`); once it has run, the `yuyao` command must be usable (for the product name see §0.4). It ships and declares all dependencies. |
| Interactive terminal interface (TUI / REPL) | `yuyao` starts an interactive session supporting the slash commands of §5, the keyboard shortcuts, and the input prefixes. For interface requirements see §0.6. |
| Non-interactive entry point `run_agent.sh` | For unattended invocation by external programs. |

#### 0.1.1 Required Interfaces

1. **The artifact root must contain**: `run_agent.sh`, `entry.py`, `wheels/` (≥1 offline wheel), `requirements.lock`, `VERSION`.
2. **`run_agent.sh` must accept and correctly handle these 7 arguments**: `--task-file`, `--repo-dir`, `--model-base-url`, `--model-name`, `--model-api-key`, `--budget-file`, `--out-dir`. The formats of the two input files are defined in `.d2a_spec/AGENT_CONTRACT.md`.
3. **The model must be reached only through `--model-base-url`** (through `D2A_MODEL_*` in the TUI); no other egress address or credential may be hardcoded.
4. **Code must be modified in place under `--repo-dir`**; execution traces and status must be written to `--out-dir` (`status.json` carrying `exit_status` and `cost_usd`, plus `.trajectory/`), and must not be written into `--repo-dir`.
5. **The step limit in `--budget-file` must be honoured** (on reaching the limit, stop cleanly and record `LimitsExceeded`); exit codes must follow §9 (`Submitted`/`LimitsExceeded` → 0, error → 1, usage error → 2).
6. **New third-party dependencies must be declared** under `[project.dependencies]` in `pyproject.toml` (or in `requirements.txt`) and must remain installable offline (the runtime environment has no network access).
7. **`install.sh` must complete the installation in one shot**, leaving the `yuyao` command usable immediately afterwards.
8. **`yuyao` must connect to the model with zero configuration**: when `D2A_MODEL_BASE_URL/NAME/API_KEY` are already present in the environment, the TUI must read them automatically, connect directly, and be usable at once.
9. **Model calls must be real**: both the TUI and headless mode must genuinely send requests to the model and return the model's actual output.
10. **The interactive TUI must be a coding agent that really executes tools, not a pure chatbot**: the model must be granted the tools of §1 (at minimum bash/read/write/edit/grep/glob), and the agent must actually execute the tool calls the model issues, feed the results back to the model, and continue. Plain conversation (e.g. `hi`) is answered with ordinary text. For the calling and execution pattern, see `agentic_turn` in `.d2a_spec/model_call_reference.py`.
11. **The standard programmatic interface `yuyao.agent_api` must be exposed**: see §0.1.2.

#### 0.1.2 Standard Programmatic Interface

> This interface allows external programs to drive the agent deterministically: construct a session → run slash commands → run one agentic turn → read observable state,
> all through this single fixed interface, without going through internal class names, constructor signatures, or turn method names.
> `deny` / `mode` / `role` take effect when passed, with the same semantics as the command-line flags and the in-session switches; when omitted, your own defaults apply.

Your package must provide an importable module `yuyao.agent_api` exposing:

```python
def create_session(*, deny=None, mode=None, role=None, cwd=None) -> AgentSession
```

The returned `AgentSession` must provide the following members:

| Member | Type / signature | Meaning |
|---|---|---|
| `.cwd` | `str` | Path of the current working directory |
| `.messages` | `list[dict]` | Message list of the active session (OpenAI style, including tool results); tool execution results must genuinely land in it |
| `.dispatch(command)` | `(str) -> str` | Execute one slash command (e.g. `"/config set k v"`, `"/ask"`, `"/mcp add x"`), with the same semantics as typing it interactively; returns text (possibly the empty string) |
| `.chat(text)` | `(str) -> str` | Run one agentic turn: tools are really executed, results are fed back to the model, which continues; returns the assistant's final text (the same "hands-on" loop as item 10 of §0.1.1) |
| `.config_get(key)` | `(str) -> Any` | Read the current value of a persistent configuration key, or `None` if unset |
| `.mcp_servers` | `list[str]` | Names of the currently configured MCP servers |
| `.close()` | `() -> None` | Clean up |

Constraints: (i) `create_session` must not issue any model call during construction (lazy connection: constructing a session with placeholder or unreachable `D2A_MODEL_*` values must itself succeed); (ii) the session must be non-streaming and non-interactive (it must not read stdin and must not enter a Live view or spinner), and `dispatch`/`chat` must return synchronously.

### 0.2 Model Connection: Unified Environment Variables

The model connection is supplied through the following unified environment variables, whose interface shape mirrors the `--model-*` arguments of `run_agent.sh`:

| Environment variable | Meaning |
|---|---|
| `D2A_MODEL_BASE_URL` | Model base URL (OpenAI-compatible gateway) |
| `D2A_MODEL_NAME` | Model name (litellm form, e.g. `litellm_proxy/<alias>`) |
| `D2A_MODEL_API_KEY` | API key used for calls |

When these three variables are present in the environment, the TUI must read them at startup, connect to the model directly, and be usable at once.

The agent must keep the model fully parameterized with zero hardcoding: headless mode reads the `--model-*` arguments of `run_agent.sh`, and the TUI reads the `D2A_MODEL_*` variables above (optionally overridden in-session by `/model`). No concrete URL, name, or key may be hardcoded, and the default model must not be an unreachable placeholder (such as `gpt-4`). When none of these variables is set, the TUI must emit a clear message rather than crash. Egress is permitted to that endpoint only.

The endpoint is the OpenAI-compatible `POST {D2A_MODEL_BASE_URL}/chat/completions`; for the calling convention see `.d2a_spec/model_call_reference.py`. The architecture is yours to design.

### 0.3 Cost Accounting

Model unit prices are injected through the environment variable `D2A_MODEL_COST`, in the format `{"input_cost_per_token": <float>, "output_cost_per_token": <float>}`. The agent must accumulate spend accordingly and write it to `cost_usd` in `status.json`; when the variable is absent, spend counts as 0 and the agent must not crash.

### 0.4 Branding and Naming

The product's only name is `yuyao agent`, and its launch command is `yuyao`. No Chinese name or other variant may be used. mini-swe-agent exists solely as an internal kernel; its name must never be visible to users.

- **Launch command**: installation provides the `yuyao` command (entering the interactive TUI); the headless contract entry point is `run_agent.sh`.
- **Use `yuyao` everywhere user-visible**: the TUI startup banner, titles, help text, prompt, version string, configuration and history file paths (e.g. `~/.yuyao/`), and the external names of packages and scripts — the string "mini" must not appear, nor may any Chinese name.
- Internal Python modules that reuse the mini kernel may keep its internal package names, but the string "mini" must not appear in user-visible output.
- The kernel's startup banner and version string must be suppressed: no text of the form `This is mini-swe-agent version X`, no migration notice, and no kernel logo may be printed to the user when `yuyao` starts or runs. Version numbers in the README, help output, and banner must be `yuyao`'s own.

### 0.5 Documentation

- **README** (at the project root, replacing the original README): centred on `yuyao agent` — project overview and positioning, installation, quick start, feature overview (corresponding to §1–§7), non-interactive use of `run_agent.sh`, and FAQ.
- **Architecture document** (a separate file, e.g. `docs/ARCHITECTURE.md`): overall design and layering, directory and module responsibilities, core data flow (an entire task from input to persisted output), how permission and role modes are realized, built-in tools and the execution environment, the persistent memory and convention-file mechanisms, model integration, the relationship between the headless and TUI entry points, trace and status output, and the relationship between `yuyao` and the internal kernel.
- Documentation must match the actual implementation, and likewise must show only `yuyao` in user-visible places.

### 0.6 Interface

- **Progress bars**: display a progress bar for long tasks, multi-step execution, installation, and batch processing (use a determinate progress bar when the total is known).
- **Loading animations**: display an animation with status text wherever the wait is of indeterminate length, such as awaiting a model response or executing a tool.
- **Streaming animation**: during streaming output (§5.5), render character by character or with a fade-in, plus a cursor animation; the thinking process may be presented collapsed (§5.5).
- **Overall look and feel**: clear sectioning and alignment, sensible colours and highlighting, visual separation of commands, output, thinking, and status, Markdown and code syntax highlighting, and conspicuous visual feedback for success, failure, and warnings; information density should be moderate and must not flood the screen.
- A mature TUI library (such as `rich`, `textual`, or `prompt_toolkit`) is recommended; the corresponding dependencies must be declared in `pyproject.toml` or `requirements.txt`.

### 0.7 Pre-delivery Self-verification

Before submission, a real install-and-run pass must be performed in a clean environment: install with `install.sh`, start the `yuyao` interactive interface using the environment variables of §0.2 and complete one task against the real model, and run headless mode through to completion with `run_agent.sh` on a toy repository.

All functionality must be genuinely implemented; no placeholders, stubs, or unimplemented branches may remain.

---

## 1. Built-in Tools (Actions the Agent May Invoke While Solving a Task)

| Tool | Meaning |
|---|---|
| shell / bash | Execute shell commands (long tasks may run in the foreground or background), producing filesystem and output effects |
| read / write / edit / multi-edit | Read / write / replace exactly / edit in bulk; the targeted content lands on disk as expected and nothing else changes |
| apply-patch | Apply a patch |
| grep / glob | ripgrep content search / glob-based file lookup |
| web-fetch / web-search | Fetch a URL / search the web (subject to `--network`) |
| browser | Browser automation (open, click, extract data, screenshot), used to verify web changes |
| read-media (image / multimodal reading) | Read and understand multimodal files such as images, screenshots, diagrams, design mockups, architecture diagrams, and screen recordings, optionally as task input; the agent must be able to describe image content and act on the information it contains |
| lsp | Go to definition / find references / diagnostics; type errors and warnings are reported automatically after each edit |
| notebook-edit | Edit a Jupyter notebook by cell (replace / insert / delete); the target cell changes as expected and other cells are untouched |
| monitor | Run a command in the background and stream each output line back in real time (tailing logs, polling CI or PR status, watching a directory for changes) without blocking the session; when an event occurs, the agent must be able to notice it mid-flight and react |
| todo / task-list | Maintain a task list with status and dependencies (pending / in_progress / completed; create, query, update, delete); it survives context compaction |
| task (sub-agent) | Spawn (mostly read-only) sub-agents to explore, with sub-task results returned (for background shell processes see bg-*) |
| bg-list / bg-output / bg-stop | Manage background shell processes (distinct from task sub-agents): list / inspect output / stop |
| memory-search | Semantic or hybrid search over memory, matching relevant historical notes even under different wording |
| memory-get | Read a given memory file or line range |
| ask-user | Ask a structured question (interactive mode), pausing for the user's answer |

> Reference syntax `@<path|symbol|agent>`: reference a file, symbol, or sub-agent in a prompt as context.

### 1.1 Tool Behaviour Contracts
| Tool | Required semantics |
|---|---|
| **read** | Images are understood as visual content (large images are downscaled automatically); PDFs can be read by page range; `.ipynb` returns each cell with its outputs; oversized files are returned paginated with a hint on how to continue with offset/limit; malformed input must fail closed — report a clean error, without crashing, looping forever, returning an inverted range, or returning empty content that fakes truncation |
| **edit** | Read before write: the file must have been read in this session before being modified, and must not have been changed externally on disk; `old_string` must match exactly and uniquely, otherwise extend the context to pin down a unique occurrence or use `replace_all`; no regular-expression or fuzzy matching |
| **multi-edit** | Multiple edits within a single tool call form one atomic operation: every hunk is first validated against the current on-disk content, and if any one fails to match, the whole call is abandoned with nothing written, leaving no intermediate state in which only the leading hunks were applied |
| **write** | An existing file must have been read before it is overwritten (new files are exempt); the write replaces the whole file rather than appending or merging; malformed input must fail closed and must not silently corrupt the content of an existing file |
| **bash** | Each command runs in its own process; a default timeout must exist, and a single command may request a longer limit when needed; output beyond the limit is spilled to a file, with the path and a preview returned; long tasks may run in the background; `cd` persists for subsequent commands within the project directory and is reset to the project root automatically when it escapes |
| **grep** | ripgrep syntax; three output modes `files_with_matches`/`content`/`count`; results can be restricted by glob or type and can span lines with `multiline`; `.gitignore` is honoured by default |
| **glob** | Results are sorted by modification time; the number of matches must be capped, and when the cap is exceeded only the results within it are returned, together with a truncation indicator |

> **Byte fidelity of changes**: the phrase "nothing else changes" for the editing tools above extends to the byte level — outside the matched region,
> line-ending sequences and byte encoding must remain byte-for-byte identical to the pre-edit content. Newly created files are exempt.

> **Multiple tool calls within one turn**: when the model issues several tool calls in a single reply,
> they must be executed one by one in the order given; each must really run and each must have a corresponding result fed back.
> Concurrent execution is not forbidden, but the observable effect on any given target must match sequential execution (e.g. a write followed by a delete ⇒ the final state is deleted).
> If one of them fails, the remaining calls must still each execute and each return their own result.

---

## 2. Top-level Commands / Sub-commands

| Invocation | Meaning |
|---|---|
| `agent` | Interactive session (REPL), accepting the slash commands of §5 |
| `agent -p "<task>"` (`--print`) | Run headless to termination, print the result to stdout, and exit per §9; never wait for input |
| `agent memory <status\|search\|index\|rem-backfill>` | Memory indexing, search, rebuild, and historical replay (see §6) |
| `agent mcp <add\|list\|remove>` | Manage MCP external tool connections; tools appear or disappear accordingly after add, remove, and list |
| `agent sandbox <list\|recreate\|explain>` | Sandbox runtime management and troubleshooting (see §3.3); `explain` states why something was blocked |
| `agent agents <list\|dispatch\|attach\|stop>` | Agent View: dispatch and monitor background sessions (each in its own git worktree), listing background sessions and their status on one screen, including which are awaiting input, with the ability to attach to or stop them |
| `agent features <list\|enable\|disable>` | Feature switches (hooks / skills / plugins / MCP) |
| `agent config <get\|set\|unset> <path> [val]` | Read and write runtime configuration with hot reload; a key change takes effect as soon as it validates, without a restart |

---

## 3. Permission Modes (Five Mutually Exclusive Levels) + Role Modes (Unified `--role`) + Sandbox / Network / Lists

### 3.1 Permission Modes (Five Mutually Exclusive Levels)
`--mode <name>`, where `name ∈ {ask, plan, auto-edit, auto-review, yolo}`; convenience aliases are also provided.

| Mode | Canonical form | Alias | Meaning |
|---|---|---|---|
| Ask (default) | `--mode ask` | (omitted) | Every mutating operation is confirmed individually; when no one is present to confirm in headless mode, mutating operations are refused |
| Plan | `--mode plan` | `--plan` | Read-only: no file changes, no mutating commands |
| Auto-edit | `--mode auto-edit` | `--auto-edit` | File edits are approved automatically; mutating shell commands still require approval |
| Auto with classifier review | `--mode auto-review` | `--auto-review` | Fully automatic plus a secondary classifier reviewing each action, blocking dangerous ones (fail-closed). Distinct from `auto-edit`, which only waives approval for edits |
| Full permissions | `--mode yolo` | `--yolo` | Full permissions, all approvals skipped; both files and commands are executed automatically |

- Compatibility aliases (semantics must be identical, §8): `--dangerously-skip-permissions` ≡ `--yes` ≡ `-y` ≡ `--yolo`.

### 3.2 Role Modes (Unified `--role <name>`, Orthogonal to Permission Modes; One Role at a Time)
`--role <name>`, where `name ∈ {execute, ask, architect, debug, orchestrator}`; `ask` and `debug` additionally have the in-session convenience aliases `/ask` and `/debug`.

| Role | Canonical form | Alias | Meaning |
|---|---|---|---|
| Execute (default) | `--role execute` | (omitted) | Carry out coding tasks normally: change code and run commands to solve the problem |
| Ask | `--role ask` | `/ask` | Answer without changing anything: explanation and Q&A, with zero modifications to the workspace |
| Architect | `--role architect` | — | A strong model produces the plan and an editing model lands the diff: plan first, then write to disk |
| Debug | `--role debug` | `/debug` | Focus on locating defects, adding diagnostics and a minimal reproduction |
| Orchestrator | `--role orchestrator [--team-name <n>]` | — | Coordinate the division of labour across multiple sub-agents (lead + teammates: a shared task list plus messages between them, with the lead splitting, assigning, and synchronizing work) |

### 3.3 Sandbox / Network / Lists (Security Switches, Orthogonal to Modes and Roles)
| Flag / setting | Meaning |
|---|---|
| `--sandbox <read-only\|workspace-write\|full-access>` | OS-level isolation level; out-of-bounds writes and privilege escalation are blocked at the restricted levels |
| `--network <on\|off>` (default off) | Whether network access is allowed; when off, network actions are refused |
| `--allow <tool/glob>` / `--deny <tool/glob>` | Allowlist and denylist for tools and paths |

> To troubleshoot "why was this tool blocked", use `agent sandbox explain` (§2); no separate flag is provided.

> **Matching criterion for deny and allow**: a rule such as `Bash(<glob>)` matches the command's **effective program name**
> — that is, the program that will actually be executed, determined after shell lexical analysis, after removing quotes and escapes, after stripping wrappers (`sh -c`/`command`/`env`/`xargs`/`exec`/
> `nice`/`timeout`/`nohup`/`stdbuf`, and the like) and path prefixes.
> Quote-split words (`to""uch`), backslashes, redundant whitespace, subshells, command substitution,
> and forms such as `find -exec` that place a command in an argument position do not change the effective program name and match all the same.
> The converse also holds: a command that merely mentions a denied string without executing it
> (such as `grep "rm " f.txt` or `echo rm`) is permitted.

> **Segment-by-segment adjudication**: a command line may consist of several segments — `;`, `&&`, `||`, pipes, subshells, and command substitution
> each introduce a segment of executable content, and each segment has its own effective program name. Adjudication applies to **every one of those segments**,
> and the two directions compose differently: deny and ask are decided by a single veto — if any segment matches, the whole command line is blocked or turned into a confirmation,
> and wrapping a refused program inside a compound command does not constitute a bypass; allow requires unanimity — only if every segment is permitted is the whole command line exempt from confirmation,
> and a single allow rule matching one segment does not permit the whole line.

> **Undetermined means not permitted**: when the effective program name cannot be determined — unbalanced quotes, failed word splitting, structures beyond the implementation's understanding,
> or any situation in which the implementation can only guess rather than compute — the command must be treated as not permitted: in interactive mode it becomes an individual confirmation,
> and in unattended mode mutating operations are refused per §3.1. Indeterminacy must not result in permission, nor may the implementation degrade to an approximation such as splitting on whitespace.

> **Data exfiltration (strong invariant)**: only content that the user has explicitly referenced through `@<path>` (§5.4) or that the agent has itself read with tools such as read or grep may enter the context and the request body; merely mentioning a file name in free text (casually saying `~/.ssh/id_rsa` or `.env`, say) must not trigger an automatic read that wires its content into any model request. The contents of sensitive files such as private keys, credentials, and `.env` must never be collected automatically or transmitted without having been named.

---

## 4. Global Flags

| Flag | Meaning |
|---|---|
| `--output-format <text\|json>` / `--json` | Output format; with json, stdout is a single parseable JSON document |
| `--json-schema <file>` | Produce a structured result per the schema; the output must conform to that schema |
| `--stream <on\|off>` (default on) | Whether output is streamed; for presentation requirements see §5.5 |
| `--show-thinking <off\|collapsed\|expanded>` (default collapsed) | Whether the model's thinking is displayed. collapsed folds it into a summary that can be expanded; expanded shows it directly; off hides it. Thinking and the final answer are visually separated |
| `-c`/`--continue` \| `--resume <id>` | Continue the most recent \| a specified session (mutually exclusive), restoring the context to carry on |
| `--no-memory` | Do not load memory or convention files for this run (§6/§7); constraints declared by the repository are not honoured |
| `--max-steps <n>` | Upper bound on run steps; on reaching it, stop cleanly and record `LimitsExceeded` without crashing |
| `--thinking <off\|low\|medium\|high\|max>` | Thinking intensity, switching the reasoning budget. The level set is defined here and shared by `/think` |
| `--auto-commit` | Commit to git automatically after each edit (with a descriptive message); each change produces one commit |
| `--auto-test` / `--test-cmd <cmd>` | Run tests automatically after editing and iterate on the results; if tests fail, keep fixing until they pass |
| `--map-tokens <n>` | Repo map injection budget; for a large repository, inject the most relevant symbols; `0` means no repo map is injected |
| `--experimental-lsp` | Enable LSP code intelligence (= the lsp tool of §1) |
| `--model <name>` | Select the model |
| `--tools <list>` \| `--allowed-tools <rules>` \| `--disallowed-tools <rules>` | Restrict the available tool set \| tools permitted without confirmation \| tools refused. Rules take forms such as `Bash(git log *)` and `Edit(/src/**)` |
| `--worktree[=<name>]` (`-w`) | Start in an isolated git worktree (created automatically or named); changes occur in the isolated checkout and never touch the main working tree |
| `--bg [task]` (+`--exec <cmd>`) | Start as a background session and return immediately, printing the session ID and releasing the terminal; with `--exec`, run a shell job in the background instead |
| `--fork-session` | On resume, create a new session ID; the original session is preserved and a new one is forked from it |
| `--max-budget-usd <n>` | Spend ceiling (headless); once cumulative spend reaches it, stop cleanly |
| `--system-prompt <txt>` \| `--system-prompt-file <f>` | Replace the system prompt |
| `--append-system-prompt <txt>` \| `--append-system-prompt-file <f>` | Append after the default system prompt, preserving the default behaviour |
| `--input-format <text\|stream-json>` | Headless input format |
| `--verbose` | Detailed per-turn output, printing the full turn-by-turn process |
| `--help` | Print the complete invocation surface |

---

## 5. In-session Interaction: Slash Commands and Keyboard Shortcuts

### 5.1 Core Slash Commands
| Command | Meaning |
|---|---|
| `/undo` | Revert the agent's most recent change (the "one step back" special case of `/rewind`); the affected files return to their pre-edit state, and the user's own uncommitted changes are left untouched |
| `/redo` | Reapply a reverted change. Any new change invalidates what could be redone — if a file was edited or created after `/undo`, `/redo` is a no-op and changes no file |
| `/rewind` (`/checkpoint`) | Open the checkpoint menu (`Esc Esc` also works on empty input): restore only the code, only the conversation, or both to a historical point, or summarize and compact the conversation from or up to that point. Every user prompt is a checkpoint, retained across sessions. `/undo` is its "one step back" special case. Limitation: only changes made by file-editing tools are tracked — files changed by bash commands (`rm`, `mv`, and so on) are not tracked and cannot be rewound |
| `/compact` (`/compress`) | Compact or summarize the context; after compaction the original task can still be carried on |
| `/plan` | Switch to read-only plan mode (= plan in §3.1) |
| `/ask` | Switch to the answer-only role (= ask in §3.2) |
| `/debug` | Switch to the debugging role (= debug in §3.2) |
| `/mode <ask\|plan\|auto-edit\|auto-review\|yolo>` | Switch permission mode in-session (mirrors `--mode`, §3.1) |
| `/role <execute\|ask\|architect\|debug\|orchestrator>` | Switch role in-session (mirrors `--role`, §3.2) |
| `/diff` | Show the current changes, listing modified files and their diffs |
| `/review [--fix] [--security\|--simplify]` | Review the current diff: by default read-only, looking for correctness bugs and cleanup opportunities; `--fix` applies findings to the working tree; `--security` focuses on security (injection, authorization, leaks); `--simplify` performs cleanup and refactoring only |
| `/security-review` | Review specifically the security risks of the current branch's changes, producing a risk list |
| `/run` | Build, launch, and drive the project's app, observing at runtime whether a change took effect rather than relying on tests alone |
| `/copy [N]` | Copy the most recent (or Nth most recent) answer to the clipboard; when it contains code blocks, a block may be selected or written to a file |
| `/focus` | Toggle the focused view: show only the latest prompt, a summary of tool calls (including diffstat), and the final answer |
| `/tui <default\|fullscreen>` | Switch the terminal UI renderer (fullscreen being flicker-free full-screen rendering), preserving the session |
| `/config` (`/settings`) | Open the settings interface: theme, editor mode (including Vim), output style, and so on; settings are visible, editable, and take effect immediately |
| `/background [prompt]` (`/bg`) | Detach the current session into a background session and release the terminal (optionally with a final instruction), monitorable through Agent View |
| `/init` | Generate the convention memory file `AGENTS.md` |
| `/memory` | View and manage cross-session memory (see §6) |
| `/tools` | List the currently available tools (= introspection over the built-in tools of §1) |
| `/recap` | Produce a one-line "where we left off" continuation summary for the session |
| `/context list` | List the size of each part of the context (raw vs injected) |
| `/context detail` | Inspect context truncation state in detail, reporting truncation and over-budget specifics |
| `/model <name>` | Switch model, preserving the context |
| `/new` | Archive the current session and start a new one |
| `/clear` (`/reset`) | Clear the current session context (without archiving; use `/new` to archive and start fresh) |
| `/sessions` (`/resume`) | List and restore past sessions |
| `/title` (`/rename`) | Rename the current session |
| `/status` | Show version, model, working directory, and current mode |
| `/export` | Export the current session as Markdown |
| `/add-dir <path>` | Add another accessible working directory to this session; files beneath it become readable and writable |
| `/cd <path>` | Change the session's working directory; subsequent operations are based on the new directory |
| `/permissions` | Manage tool permission rules interactively (allow / ask / deny, = the lists of §3.3) |
| `/hooks` | View and manage lifecycle hook configuration (= `hooks.json`, §7) |
| `/mcp` | Manage MCP connections (= the in-session mirror of `agent mcp`, §2) |
| `/agents` | Manage sub-agent configuration (= task in §1 / orchestrator in §3.2) |
| `/help` | List all supported commands |
| `/exit` (`/quit`) | Leave the session; the process ends with exit code 0 |

### 5.2 Advanced
| Command | Meaning |
|---|---|
| `/goal <condition>` | Goal autonomy: once a completion condition is set, keep working and judge attainment independently, until the condition is met or a limit is reached |
| `/loop <prompt> [--interval <s>] [--max <n>] [--until <cond>]` | Run a fixed prompt in a loop or as a poll within the session. Unlike `/goal` (which judges completion autonomously), `/loop` repeats the same prompt; it stops on the `--until` condition, the `--max` count, or `Esc` |
| `/fork` | Spawn a background sub-agent (inheriting the current context, returning its result, and not polluting the original). To fork the conversation, use `/branch` |
| `/branch [name]` | Fork the conversation at the current point and switch into the copy, preserving the original, which remains reachable through `/sessions` |
| `/btw <question>` (`/side`) | Quick aside: the exchange is not recorded in the main conversation history, so the main history does not grow |
| `/verify` | Build, run, drive the app, and observe, confirming that the change meets the bar (at runtime, not by tests or type checks alone) |
| `/batch <instruction>` | Split a large change into independent units, each implemented in parallel by a sub-agent in its own worktree (= the concrete workflow of `--role orchestrator`) |
| `/workflows` (`/workflow <name>`) | Dynamic workflows: scripted orchestration of many sub-agents that cross-check each other's results, for multi-round tasks such as very large changes, whole-repository audits, and large-scale migrations; lists running and completed workflows with their current stage and each sub-agent's progress, and can run a named workflow |
| `/skills` \| `/skill <name>` | List skill packages (SKILL.md) \| trigger execution of a named skill |
| `/flow:<name>` | Execute an embedded skill flow graph, node by node |
| `/queue <steer\|followup\|collect\|interrupt>` | Queue, steer, or abort with a message mid-stream |
| `/tasks` (`/bashes`) | View and manage everything running in the background (background shell processes and tasks: list, inspect, stop, in concert with bg-* in §1) |
| `/think <level>` | Set the thinking level (the same level set as `--thinking`, §4) |
| `/show-thinking <off\|collapsed\|expanded>` | Switch how thinking is displayed in-session (mirrors `--show-thinking`; distinct from `/think`, which sets the reasoning budget) |
| `/stream <on\|off>` | Toggle streaming output in-session (mirrors `--stream`) |
| `/usage <off\|tokens\|full>` | Toggle token and usage display |

> **Delineating sub-agents, orchestration, and background execution**:
> - `task` (the §1 tool) = spawn a read-only sub-agent temporarily during problem solving to explore; `/fork` = spawn a background sub-agent that inherits the context and returns its result.
> - `--role orchestrator` (the §3.2 role) = adopt the working posture of coordinating multiple sub-agents; `/batch <instruction>` = its concrete workflow (split a large change into units, each in its own worktree, run in parallel).
> - `/branch` = fork the conversation (not an agent); `/tasks` + `bg-*` = manage background shell processes and tasks (not agents); `/background` (`/bg`) = move the entire current session into the background.
> - `/agents` (§5.1) = manage sub-agent configuration (does not spawn); `agent agents` (§2, Agent View) = dispatch and monitor background sessions (each with its own worktree), as distinct from `/tasks`, which manages the current session's background items.
> - `/loop` = loop or poll on a prompt within one session (does not spawn agents); `/workflows` = scripted orchestration of many sub-agents with cross-checking (larger in scale and multi-round compared with `/batch`).
> - Parallel work is isolated with git worktrees (§7), preventing multiple agents from editing the same file.

### 5.3 Keyboard Shortcuts (Interactive Mode)
| Shortcut | Action |
|---|---|
| `Enter` | Submit the input; the current input is submitted as one turn |
| `Shift+Enter` / `Ctrl+J` | Insert a newline in the input; multi-line input is not submitted |
| `Shift+Tab` | Toggle between plan (read-only) and non-plan (= plan in §3.1) |
| `Ctrl+G` | Invoke `$EDITOR` to edit the input in an external editor |
| `Ctrl+V` | Paste an image or media file into this turn's context (in concert with read-media, §1) |
| `Ctrl+S` | Steer the running turn, injecting a mid-flight message into it (= `/queue steer`, §5.2) |
| `Esc` | Stop or cancel the current run |
| `Ctrl+C` | Interrupt the run / clear the input |
| `Ctrl+D` | Exit when the input is empty, with exit code 0 |
| `Ctrl+O` | Expand or collapse tool output (transcript viewer) |
| `Ctrl+T` | Expand or collapse the task list view |
| `Alt+T` | Toggle thinking display (= `/show-thinking`, collapsed ↔ expanded) |
| `Ctrl+E` | Expand or collapse the plan or preview |
| `Ctrl+R` | Reverse-search the input history, searching interactively and reusing an entry |
| `Ctrl+B` | Move the running bash command to the background and carry on the conversation |
| `Ctrl+X Ctrl+K` | Terminate all background sub-agents of this session (press twice to confirm) |
| `Esc Esc` | Open the rewind checkpoint menu on empty input; with text present, clear the draft and save it to history |
| `Ctrl+Z` / `Ctrl+Shift+Z` | Undo / redo an input edit |
| `Ctrl+L` | Clear the screen: clear only the terminal display, leaving the context untouched (as distinct from `/clear`, which clears the context) |
| `\` + `Enter` | Multi-line input (works in any terminal); does not submit but moves to the next line |

### 5.4 Input Prefixes (`/` `@` `!`)
| Prefix | Meaning |
|---|---|
| `/<command>` | Slash command (§5.1/§5.2), recognized only at the start of a message |
| `@<path\|symbol\|agent>` | Reference injection: a file, a directory (recursively), a symbol, or a sub-agent as context |
| `!<shell command>` | The user executes a shell command directly (as distinct from the agent's bash tool); the command runs in a subshell and its output is echoed; a bare `!` toggles shell mode |

### 5.5 Thinking Display / Streaming Presentation / Image Reading
| Capability | Meaning |
|---|---|
| Thinking is visible and collapsible | The agent's thinking is presented according to the `--show-thinking`/`/show-thinking`/`Alt+T` setting: `collapsed` folds it by default into a one-line summary expandable with a single key; `expanded` shows it directly; `off` hides it. Thinking, the final answer, and tool calls are visually separated and never conflated |
| Streaming output plus animation | `--stream on` (the default): model output is rendered incrementally token by token in real time, accompanied by a streaming animation; a stream can be aborted with `Esc` and steered with `Ctrl+S` or `/queue steer` |
| Conversation is not an unbounded agentic loop | Ordinary conversation and questions (greetings, queries) should receive a fast streaming answer; only an explicit coding task enters the multi-step "think → tool → observe" loop, with every step's action and output visible in real time |
| Non-streaming output | `--stream off`: the whole passage is emitted at once after generation completes, with no character-by-character animation, suiting pipes, logs, and TTY-less environments |
| Images as input | Images may be supplied through `@<image path>`, a `Ctrl+V` paste, or headless input, letting the agent understand screenshots, error images, design mockups, and architecture diagrams and solve the task from them; the TUI must show a visible indicator for each image taken in |

### 5.6 Vim Editing Mode (Input Box)
Enabled through `/config` → editor mode; the input box then supports vim-style editing:
- **Modes**: NORMAL / INSERT / VISUAL (`Esc` enters NORMAL, `i/I/a/A/o/O` enter INSERT, `v/V` enter VISUAL).
- **Motion**: `h/j/k/l`, `w/e/b`, `0/$/^`, `gg/G`, `f/F/t/T` plus `;`/`,`.
- **Editing**: `x`, `dd/D`, `dw/de/db`, `cc/C/cw`, `yy/yw`, `p/P`, `>>/<<`, `J`, `u`, `.` (repeat).
- **Text objects**: `iw/aw`, `i"/a"`, `i(/a(`, `i[/a[`, `i{/a{` in combination with `d/c/y`.
- Once enabled, the input box responds with the vim semantics above; when disabled, it performs ordinary line editing.

### 5.7 Interaction Enhancements
| Capability | Meaning |
|---|---|
| Command history and reverse search | Input history is saved per working directory; `↑/↓` browses it and `Ctrl+R` reverse-searches and reuses an entry; consecutive identical submissions are recorded once |
| Suggestions | After a turn, show a greyed-out next-step suggestion in the input box (predicted from the session and history); `Tab`/`→` accepts it and typing dismisses it |
| Session continuation summary | On returning after a while, show a one-line "where we left off" summary; `/recap` generates one on demand |
| PR status bar | When the current branch has an open PR, the footer shows a clickable PR link with a colour-coded state (passing, awaiting review, changes requested, draft), refreshed periodically |
| Focused and full-screen rendering | `/focus` for a condensed view, `/tui fullscreen` for flicker-free full-screen rendering (see §5.1) |

---

## 6. Persistent Memory System

> Memory = plain-text files in the workspace + search tools + automatic maintenance. Once written to persistent memory, content must land on disk
> (location and format are yours to choose) and must remain readable back, or recallable through `memory-search`, after `/new`, `/clear`, and in new sessions
> (`--no-memory` excepted).

### 6.1 Memory Files
| File | Purpose | Loading / indexing rule |
|---|---|---|
| `MEMORY.md` | Long-term memory (distilled facts, preferences, decisions) | Loaded automatically at the start of every session; when it exceeds the bootstrap budget, the on-disk copy stays complete and the injected copy is truncated |
| `memory/YYYY-MM-DD.md` | Daily working memory (run context, notes) | Today's and yesterday's are loaded automatically; all are indexed by the search tools rather than injected every turn |
| `DREAMS.md` | Consolidation and review journal (output of dreaming) | For human review; written by the consolidation process |

### 6.2 External Invocation of Memory
| Invocation | Meaning |
|---|---|
| Natural language "Remember that …" | Triggers an automatic write, into `MEMORY.md` or the day's `memory/*.md` |
| `/remember <text>` | Explicitly write one persistent memory entry into `MEMORY.md` |
| `/forget <query>` | Delete matching automatic memory entries. Matching is per entry: only the entry corresponding to the query is removed and every other entry is preserved individually; other entries whose body contains the same substring are not deleted along with it (forgetting `alpha`, for instance, does not affect another entry reading `alphabet …`). Case sensitivity and support for fuzzy matching are left to the implementation |
| `/dream` | Trigger memory consolidation manually (the manual form of dreaming, §6.3), promoting qualifying short-term signals into `MEMORY.md` |
| Tool `memory-search "<q>"` / `agent memory search` | Semantic or hybrid search, matching relevant historical notes even under different wording |
| Tool `memory-get <file[:lines]>` | Read the specified memory content |
| `agent memory status` | Print index readiness and the provider |
| `agent memory index --force` | Rebuild the index |
| `agent memory rem-backfill [--rollback]` | Replay historical notes: stage promotion candidates, with the option to roll back and remove them |
| `/context list` · `/context detail` | Inspect raw and injected sizes and truncation state |
| Editing `MEMORY.md`/`memory/*.md`/`DREAMS.md` directly | Edit memory by hand; the corresponding memory changes immediately |

### 6.3 Automatic Maintenance
- **Automatic memory-flush before compaction (on by default)**: when the context is about to fill up, run a silent turn before compaction to persist important information into memory files, so that old facts remain recallable afterwards.
- **Distillation**: promote useful content from daily notes into `MEMORY.md` and delete stale entries.
- **Dreaming (off by default, may be enabled by the user)**: periodic background consolidation that promotes only qualifying short-term signals into `MEMORY.md` per thresholds (score, recall frequency, query diversity), writing stage summaries to `DREAMS.md`.

### 6.4 Memory-related Configuration Keys
`memorySearch.provider` (embedding provider, trading off search quality) / `compaction.memoryFlush.model` (pin the flush turn to a specified or local model) / the memory backend is selectable (built-in / local sidecar / cross-session memory service, and so on).

---

## 7. Convention Files

| File / directory | Purpose |
|---|---|
| `.trajectory/` | Execution trace (mandatory): every run records its complete trace to `.trajectory/` (one per run), logging each step's action (tool call, command, edit) with its input and result. The format is the agent's own choice (plain text, JSONL, or split across files). Location: interactive mode writes to `<cwd>/.trajectory/` under the current working directory; headless mode writes to `<out-dir>/.trajectory/`, outside the repository being modified, so as not to pollute the patch |
| `AGENTS.md` (falling back to `CLAUDE.md`) | Operating instructions and memory conventions, read and honoured automatically; verifiable constraints written there are honoured, except under `--no-memory` |
| `MEMORY.md` / `memory/*.md` / `DREAMS.md` | Memory files, see §6 |
| `.plans/` | Editable plans produced by plan mode; after planning, a plan file appears that may be edited and then executed |
| `hooks.json` | Lifecycle hooks (able to intercept before and after execution); quality gates such as "tests or lint must pass before completion" can be configured. If `beforeShellExecution` returns deny, the command does not run; if a quality gate fails, the run is not allowed to finish |
| `plugins/<name>/plugin.json` | Plugins: declare a group of extensions that can be enabled or disabled as a whole (which may include custom commands, skills, and hooks). After `agent features disable plugins`, none of the extensions they declare takes effect |
| `SKILL.md` (`skills/<name>/`) | Trigger-based skill packages (agentskills standard): a trigger keyword loads and executes the skill |
| Custom commands (`commands/<name>.md`) | User-defined slash commands: Markdown plus YAML frontmatter, supporting `{{args}}` parameters, `@{file}` injection, and `!{cmd}` dynamic injection; subdirectories map to `:` namespaces (`/git:commit`). Dropping in the file makes `/<name>` available |
| `agents/<name>.md` (custom sub-agents) | Define reusable specialized sub-agents: constrain their role and available tool set. Dropping in the definition makes it invocable by name through `/agents` or `task`, running under the declared tool restrictions |
| git worktree (parallel isolation) | Parallel sub-agents and background sessions each use their own git worktree, so they never touch the same file |
| Rule files (glob-conditioned rules) | Rule injection triggered by path or condition; when a path matches, the rule takes effect |
| Permission list configuration (allow/deny) | Tools and paths permitted or refused by default |
| MCP configuration | Declare external MCP servers; once configured, additional tools appear |

---

## 8. Consistency Rules

> The following rules constrain the relationships between the features already listed in the preceding sections; their effect must land on an externally observable surface.
> "External" means one of: the request sent to the model, files on disk, the process exit code, or the command's own output — any one of the four suffices.
> The implementation is not constrained: no particular function need be exposed, and no wording, layout, or field name is prescribed for any output.

1. **Aliases and shortcuts are synonymous triggers**: every convenience alias (`--plan`/`--auto`/`--yolo`/`/ask`/`/debug`/`--dangerously-skip-permissions`/`--yes`/`-y`, and so on) and every keyboard shortcut (such as `Ctrl+S` = `/queue steer`, `Shift+Tab` = toggle plan, `Esc` = abort, `Alt+T` = toggle thinking display, `Ctrl+T` = toggle the task list, `Esc Esc` = rewind) must behave exactly as its canonical long form and must not introduce new semantics.

2. **In-session switches mirror the startup flags**: the semantics of switching with `/mode`, `/role`, `/stream`, and `/show-thinking` (and the aliases `/plan`/`/ask`/`/debug`) in-session must be identical to those of the corresponding startup flags; the switch takes effect immediately and is subject to the same constraints as the startup flag.

3. **The current mode must be discernible in the request**: after a permission mode or role mode switch, the request sent to the model must itself reveal which level is in force.

4. **Relationship of the three axes**: permission modes, role modes, and the security switches form three orthogonal axes that may be combined freely; conflicting combinations (such as `--role ask` together with a demand to modify files) resolve in favour of read-only. `yolo` only stops prompting for approval; it remains bound by `AGENTS.md` and by `--sandbox`/`--network`/`--deny`.

5. **Thinking budget ⟂ thinking display**: `--thinking`/`/think` adjust the reasoning budget and intensity; `--show-thinking`/`/show-thinking`/`Alt+T` adjust whether thinking is displayed — the two are independent and must not override each other.

6. **Unknown invocations must fail explicitly**: an unknown flag or slash command → an explicit error (headless exit code 2; a message in interactive mode).

7. **Accepted means effective and readable back**: if a configuration write is accepted (no error reported), the new value must be retrievable through the corresponding read command. Refusing an unrecognized configuration key is legitimate (an explicit error suffices); an accepted write, however, must not read back as the old value or the default.

8. **External tool connections: both surfaces must support add and remove, and listing must reflect the change**. The command-line sub-commands of §2 and the in-session commands of §5 must both support the three actions register, remove, and list. After an add or remove on either surface, the listing on that same surface must reflect the change. The argument form is yours to choose.

9. **Requests sent to the model must always be well-formed**: every request sent to the model must satisfy — exactly one system prompt, placed first; no two adjacent messages with the same role; no tool call separated from its response; no empty content block and no empty tool result; and no illegal bytes (nulls, lone surrogates, and the like). This constraint applies to all requests, without exception.

---

## 9. Exit Codes (Closed Set, Mandatory for Headless Mode)
`0` clean termination (`Submitted` completed / `LimitsExceeded` limit reached); `1` runtime error; `2` usage error (unknown or conflicting flag, unknown sub-command or sub-action, missing argument).
- Self-reported status: headless mode writes `status.json` under `--out-dir` (`exit_status`: `Submitted`/`LimitsExceeded` correspond to exit code 0, errors to 1; including cumulative spend `cost_usd`, with unit prices per §0.3) plus `.trajectory/` (the trace, format of your choosing, see §7).
- **`Submitted` must be earned**: only genuine completion of the task (with an actual result or tool effect) may be recorded as `Submitted`; when the model returns empty content, a degenerate response, or takes no real action, `Submitted` must not be recorded and `LimitsExceeded` or an error state must be recorded instead.
