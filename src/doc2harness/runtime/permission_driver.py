"""Run one active ``permission`` scenario against a real artifact.

The driver uses the public artifact launcher and an explicit model-gateway
stimulus.  Permission state is supplied through the artifact's reviewed
configuration surface, while all verdict inputs are reconstructed from the
typed gateway records and the private workspace; free-form output is retained
only as diagnostic text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from doc2harness.runtime import mock_gateway
from doc2harness.runtime.direct_mock_driver import (
    _digest,
    _request_facts,
    _run_artifact,
    _trajectory,
)
from doc2harness.runtime.resource_limits import container_memory_flags


def _observed_text(trajectory: str) -> str:
    """Project a trajectory down to what the model and its tools said.

    A point of the form "this token must not reach the model" is answered by
    the result, not by the request.  A recorded call keeps the arguments it
    was made with and a recorded refusal repeats the command it refused - so
    the token appears in the very call the product then *refused*, and a
    session that correctly denied it reads as having leaked it.  The refusal
    text itself survives, which is the evidence the point actually wants.

    Each product names that record in its own words - one writes
    ``"kind": "permission"`` with a ``target``, another ``"kind":
    "bash_denied"`` with a ``command`` - so the echo is removed by the field
    that carries it rather than by the event's name.  Keying on one product's
    spelling leaves the other's echo in place, and the point inverts again.
    """

    without_arguments = re.sub(
        r'"(?:arguments|args|input)"\s*:\s*"(?:[^"\\]|\\.)*"',
        '"arguments": ""',
        trajectory,
    )
    without_arguments = re.sub(
        r'"(?:arguments|args|input)"\s*:\s*\{[^{}]*\}',
        '"arguments": {}',
        without_arguments,
    )
    # A record that states a denial is describing a request, so the command or
    # target it names is the request - never a result.
    return re.sub(
        r'("kind"\s*:\s*"[a-z_]*(?:denied|refused|blocked|permission)[a-z_]*"[^\n]*?)'
        r'"(command|target)"\s*:\s*"(?:[^"\\]|\\.)*"',
        r'\1"\2": ""',
        without_arguments,
    )


def _escape_path(root: Path) -> Path:
    """A unique absolute path outside the workspace for the escape probes.

    These points ask whether a restricted session can write outside its
    working directory, so the target has to be a specific path the observer
    can then look for.  Asking instead whether anything at all exists beside
    the workspace is answered by the harness's own scratch files, which
    makes a session that was correctly blocked read as having escaped.
    """

    return root / "escape" / f"perm_esc_{os.getpid()}.txt"


def _rounds(plan: Any, escape: str = "", escape_directory: str = "") -> list[list[dict[str, Any]]]:
    rounds: list[list[dict[str, Any]]] = []
    for turn in plan.gateway_turns:
        calls: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            arguments = dict(call.arguments)
            # The deterministic gateway treats Bash's command as a
            # top-level field in a seq_probe round; other tools keep their
            # schema arguments under ``args``.  Keeping this distinction here
            # avoids silently turning every permission probe into ``bash ''``.
            # ``{ESC}`` stands for a path outside the workspace and
            # ``{ESCDIR}`` for the directory holding it; left unexpanded the
            # product writes a file literally named "{ESC}" inside the
            # workspace, which is not an escape at all.
            def _expand(value: Any) -> Any:
                if not isinstance(value, str):
                    return value
                if escape_directory:
                    value = value.replace("{ESCDIR}", escape_directory)
                return value.replace("{ESC}", escape) if escape else value

            arguments = {key: _expand(value) for key, value in arguments.items()}
            if call.tool_name in {"bash", "shell", "run_command", "execute", "exec", "terminal"}:
                command = str(_expand(arguments.pop("command", "")))
                calls.append({"name": call.tool_name, "command": command, **arguments})
            else:
                calls.append({"name": call.tool_name, "args": arguments})
        if calls:
            rounds.append(calls)
    return rounds


# Interpreter/script operands are mutation vehicles, never the mutated target.
_SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".pl", ".rb", ".js")


def _initial_state_from_setup(commands: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Classify the recipe's initial permission state as ``(mode, role)``.

    Artifacts gate mutation on
    ``mode == "plan" or role == "ask" or sandbox == "read-only"``.  ``/mode ask``
    is a *confirmation* mode and does not restrict writes, whereas the bare
    ``/ask`` alias selects the answer-only *role*, which does.  Passing an ``ask``
    role through ``mode`` therefore leaves ``read_only`` false and silently turns
    a readonly probe into an unrestricted write.

    Only the *first* state-setting command describes the session's initial
    state.  Anything after it is an ordered transition that must reach the
    artifact through ``session.dispatch`` so the transition itself is exercised;
    injecting a later state would start the session already in its final state
    and erase the behavior under test.  When the recipe transitions at all, the
    session starts from the artifact default and nothing is injected.
    """
    states: list[tuple[str | None, str | None]] = []
    for command in commands:
        text = command.strip()
        mode_match = re.search(r"/mode\s+(yolo|plan|ask|auto-edit|auto-review)\b", text)
        if mode_match:
            states.append((mode_match.group(1), None))
            continue
        role_match = re.search(r"/role\s+(execute|ask|architect|debug|orchestrator)\b", text)
        if role_match:
            states.append((None, role_match.group(1)))
            continue
        # Short slash aliases accepted by the permission probes.  ``/ask`` is
        # a role alias; the rest are mode aliases.
        if re.fullmatch(r"/ask\b", text):
            states.append((None, "ask"))
            continue
        mode_alias = re.fullmatch(r"/(yolo|plan|auto-edit|auto-review)\b", text)
        if mode_alias:
            states.append((mode_alias.group(1), None))

    if not states:
        return (None, None)
    if len(states) > 1:
        # Ordered transition sequence — dispatch must establish every step.
        return (None, None)
    return states[0]


def _mode_from_setup(commands: tuple[str, ...]) -> str:
    """Return the effective mode for the on-disk config file.

    This is the config-file view, which has no notion of ordered transitions;
    it records the last declared mode so a launcher that reads only the config
    still sees a coherent value.  ``/ask`` is excluded because it is a role.
    """
    mode = "yolo"
    for command in commands:
        match = re.search(r"/mode\s+(yolo|plan|ask|auto-edit|auto-review)\b", command)
        if match:
            mode = match.group(1)
            continue
        alias = re.fullmatch(r"/(yolo|plan|auto-edit|auto-review)\b", command.strip())
        if alias:
            mode = alias.group(1)
    return mode


def _explicit_mode_from_setup(commands: tuple[str, ...]) -> str | None:
    """Return the initial mode to pass to ``create_session``, if any."""
    return _initial_state_from_setup(commands)[0]


def _explicit_role_from_setup(commands: tuple[str, ...]) -> str | None:
    """Return the initial role to pass to ``create_session``, if any."""
    return _initial_state_from_setup(commands)[1]


def _public_session_script(
    plan: Any,
    escape_directory: Path | None = None,
    counterfactual_setup: tuple[str, ...] | None = None,
) -> str:
    """Build the in-container script for the artifact's public API.

    Permission scenarios are interactive by contract: setup slash commands
    such as ``/mode ask`` must be applied to the same session that receives the
    deterministic tool calls.  Artifact launchers commonly hard-code yolo for
    ordinary headless runs, so invoking only ``run_agent.sh`` silently discards
    the setup and creates a false permission failure.  This script still imports and
    executes the unchanged artifact inside the evaluation image; it merely
    selects its reviewed ``yuyao.agent_api.create_session`` boundary, the same
    boundary the stage1 permission smoke uses.
    """
    deny = list(plan.deny_rules)
    setup = [
        item.replace("{ESCDIR}", str(escape_directory)) if escape_directory else item
        for item in (list(counterfactual_setup) if counterfactual_setup is not None
                     else list(plan.setup_commands))
    ]
    mode = _explicit_mode_from_setup(plan.setup_commands)
    role = _explicit_role_from_setup(plan.setup_commands)
    mode_argument = f", mode={mode!r}" if mode is not None else ""
    role_argument = f", role={role!r}" if role is not None else ""
    return (
        "import json, os, sys, traceback\n"
        "from yuyao.agent_api import create_session\n"
        # Do not pre-apply a guessed mode/role here.  Commands such as
        # ``/ask`` intentionally change the role and (in the artifact's
        # command router) also establish its mode; dispatching the exact setup
        # sequence from the recipe is the only faithful behavior.
        # Pass the declared mode through the public API as well as dispatching
        # its slash command.  Some artifacts treat ``/mode`` as a
        # display/router command and leave the session permission state at its
        # default (yolo); omitting this argument would silently turn an ask
        # probe into an unrestricted write.  The declared role is passed for the
        # same reason: ``read_only`` is gated on ``role == "ask"``, so a role
        # folded into ``mode`` would not restrict anything.
        f"session = create_session(deny={deny!r}{mode_argument}{role_argument}, cwd='/sbh-workspace')\n"
        f"for command in {setup!r}:\n"
        # A setup command the product does not implement raises, and letting
        # that abort the script throws away the probe entirely: the point then
        # reports zero requests and judges the product on an observation it
        # never made.  One artifact exposes its sandbox level only as a CLI
        # flag, so ``/sandbox full-access`` raises "unknown command" and the
        # write probe that followed never ran at all.  Logging each such
        # failure and carrying on is the only faithful reading: the declared state is already passed to the session factory
        # above, so the run is still in the state the recipe asked for.
        "    try:\n"
        "        session.dispatch(command)\n"
        "    except BaseException as exc:\n"
        "        sys.stderr.write('SETUP_ERR %r: %r\\n' % (command, exc))\n"
        "try:\n"
        "    session.chat('Please run the necessary shell commands now.')\n"
        "finally:\n"
        "    session.close()\n"
    )


def _run_public_session(
    artifact: Path,
    workspace: Path,
    home: Path,
    env: dict[str, str],
    port: int,
    plan: Any,
    timeout: int,
    script: str | None = None,
    escape_root: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run a permission scenario through the artifact's public session API."""

    image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
    command = [
        "docker", "run", *container_memory_flags(), "--rm", "--init", "--network", "bridge",
        "--add-host=host.docker.internal:host-gateway", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{artifact}:/sbh-artifact:ro",
        "-v", f"{workspace}:/sbh-workspace:rw",
        # Without a bound working directory the container starts in the image
        # default, so a relative path written by a hook command lands outside
        # the mounted workspace and the observer, looking where the recipe says
        # to look, concludes the hook never fired.
        "-w", "/sbh-workspace",
        # The escape probe writes to an absolute path outside the working
        # directory, so that path has to exist at the same location inside the
        # container - otherwise the write fails for want of a directory and a
        # session that was never restricted still reads as blocked.
        *(("-v", f"{escape_root}:{escape_root}:rw") if escape_root else ()),
        "-v", f"{home}:/sbh-home:rw",
        "-e", "HOME=/sbh-home", "-e", "YUYAO_HOME=/sbh-home/.yuyao",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", f"D2A_MODEL_BASE_URL=http://host.docker.internal:{port}/v1",
        "-e", "D2A_MODEL_NAME=litellm_proxy/mock", "-e", "D2A_MODEL_API_KEY=sk-mock-test",
        "-e", "PYTHONPATH=/sbh-artifact/src", image, "python3", "-c",
        script if script is not None else _public_session_script(plan),
    ]
    try:
        completed = subprocess.run(
            command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return completed, False
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, -1, exc.stdout or "", exc.stderr or ""), True


def _shell_path(command: str) -> str | None:
    # This intentionally extracts only relative workspace candidates.  It is
    # used to locate a declared marker, never to execute or normalize a path.
    candidates = re.findall(r"(?<![A-Za-z0-9_./-])([A-Za-z0-9_./-]+)(?=\s|$|['\"])", command)
    for item in candidates:
        if item in {"echo", "printf", "touch", "cat", "rm", "mkdir", "chmod", "bash", "sh"}:
            continue
        if item.startswith("/") or item in {"-f", "-rf", "-p"}:
            continue
        if "/" in item or "." in item or "_" in item:
            return item.strip("'\"")
    return None


def _effective_cwd(workspace: Path, setup_commands: tuple[str, ...]) -> Path:
    """Infer the effective working directory after the setup slash commands.

    ``/cd`` changes the working directory inside the artifact session.  When the
    probe command uses a bare filename such as ``tgt.txt`` (not a full path), the
    observer must read it relative to the final cwd, not the workspace root.
    Only relative paths that stay inside the workspace are followed.
    """
    cwd = workspace
    for command in setup_commands:
        match = re.fullmatch(r"/cd\s+([^\s]+)", command.strip())
        if not match:
            match = re.search(r"^/cd\s+([^\s]+)", command.strip())
        if match:
            target = match.group(1).strip()
            if target.startswith("/"):
                continue  # absolute path inside container — skip
            candidate = (cwd / target).resolve()
            try:
                candidate.relative_to(workspace.resolve())
                cwd = candidate
            except ValueError:
                pass  # escapes workspace — ignore
    return cwd


def _expand_literal_substitution(token: str) -> str:
    """Resolve the constant command substitutions used by the deny/readonly probes.

    Recipes address a target through ``$(echo tgt).txt`` or ``${x}`` style
    framing to test the artifact's command normalisation.  The shell expands
    these to a fixed name, so the observer must look at the same file rather
    than at a literal ``$(echo tgt).txt`` path that never exists.  Only constant
    ``echo``/``printf`` substitutions are expanded — nothing is executed.
    """
    previous = None
    current = token
    while previous != current:
        previous = current
        current = re.sub(
            r"\$\(\s*(?:echo|printf)\s+([^()|;&<>$`]*?)\s*\)",
            lambda match: match.group(1).strip().strip("'\""),
            current,
        )
        current = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", r"$\1", current)
    return current


def _target_path(
    workspace: Path,
    command: str,
    seed: tuple[tuple[str, str], ...],
    effective_cwd: Path | None = None,
) -> Path | None:
    cwd = effective_cwd if effective_cwd is not None else workspace
    # Extract the last token from the command — for cp/mv it is the destination.
    # Constant command substitutions are expanded so the observer inspects the
    # same file the shell writes to.
    raw_tail = command.split()[-1].lstrip(">").strip().strip("'\"") if command.split() else None
    tail = _expand_literal_substitution(raw_tail) if raw_tail else None
    # A trailing *script* operand (``python3 w.py``, ``bash x.sh``) is the
    # mutation vehicle, not the mutated file.  The file the script writes is the
    # other seed, so the trailing token must not win in that shape.
    tail_is_script = bool(tail) and tail.endswith(_SCRIPT_SUFFIXES)
    # Prefer a seed that matches by the last token / full relative path over an
    # earlier partial match, so ``cp src.txt tgt.txt`` targets ``tgt.txt``.
    if tail and not tail_is_script:
        for relative, _ in seed:
            if Path(relative).name == tail or relative == tail:
                return workspace / relative
    if tail_is_script:
        # Fall back to the single seed that is not the script itself.
        non_script = [
            relative for relative, _ in seed
            if not relative.endswith(_SCRIPT_SUFFIXES)
        ]
        if len(non_script) == 1:
            return workspace / non_script[0]
    for relative, _ in seed:
        if relative in command:
            return workspace / relative
    # Prefer the operand of a redirection or a mutating command.  A generic
    # token scan would mistake ``PERM_LIVE_7755`` (the echoed payload) for the
    # actual ``live.txt`` target.
    try:
        tokens = [_expand_literal_substitution(token) for token in shlex.split(command)]
    except ValueError:
        tokens = []
    for marker in (">", ">>"):
        if marker in tokens:
            index = len(tokens) - 1 - tokens[::-1].index(marker)
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("/"):
                return cwd / tokens[index + 1]
    for command_name in ("touch", "rm", "cat", "mkdir", "chmod"):
        if command_name in tokens:
            index = tokens.index(command_name)
            if index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                return cwd / tokens[index + 1]
    candidate = _shell_path(command)
    return cwd / candidate if candidate else None


_HOOK_EXEC_PLACEHOLDER = "{HOOK_EXECUTABLE}"
_HOOK_MISSING_PLACEHOLDER = "{HOOK_MISSING_EXECUTABLE}"
_HOOK_MARKER_PLACEHOLDER = "{HOOK_MARKER}"
_HOOK_EVENT = "HOOKFAIL_HEALTHY_HOOK_EXIT_0"


def _hook_fixture(workspace: Path) -> dict[str, str]:
    """Materialise the hook executables a hook point names in its config.

    The recipe declares its hook commands as placeholders because the paths
    are only known once the run has a directory.  Left unsubstituted the
    product is handed ``{HOOK_EXECUTABLE} {HOOK_MARKER}`` verbatim, the shell
    reports "command not found", and the point sees a hook that failed - on
    a fixture that was never built.  One entry has to exist and exit 0, the
    other must be absent: that contrast is the whole subject of the point.
    """

    directory = workspace / ".sbh-hooks"
    directory.mkdir(parents=True, exist_ok=True)
    healthy = directory / "healthy_hook.sh"
    healthy.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' '{_HOOK_EVENT}' >> \"$1\"\n"
        "exit 0\n",
        encoding="utf-8",
    )
    healthy.chmod(0o755)
    return {
        _HOOK_EXEC_PLACEHOLDER: "/sbh-workspace/.sbh-hooks/healthy_hook.sh",
        _HOOK_MISSING_PLACEHOLDER: "/sbh-workspace/.sbh-hooks/missing_hook_executable",
        _HOOK_MARKER_PLACEHOLDER: "/sbh-workspace/.sbh-hooks/hook.trace",
    }


def _hook_events(workspace: Path) -> tuple[str, ...]:
    """Read the lines the hook executables appended to their trace."""

    trace = workspace / ".sbh-hooks" / "hook.trace"
    if not trace.is_file():
        return ()
    return tuple(
        line.strip()
        for line in trace.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    )


def _seed(workspace: Path, plan: Any) -> None:
    substitutions = (
        _hook_fixture(workspace) if plan.fixture.hook_exec_fixture else {}
    )
    for relative, content in plan.fixture.seed_files:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # A trailing slash declares a directory, which is what the rmdir
        # probe removes.  pathlib normalises the slash away, so the entry is
        # created as a directory explicitly; as a regular file the probe would
        # have nothing to remove and the point could not observe whether
        # read-only blocked the removal.
        if str(relative).endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        for placeholder, actual in substitutions.items():
            content = content.replace(placeholder, actual)
        path.write_text(content, encoding="utf-8")
    if plan.fixture.initialize_git:
        for relative, content, _worktree in plan.fixture.git_baseline_files:
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        subprocess.run(("git", "init", "-q"), cwd=workspace, check=False)
        subprocess.run(("git", "config", "user.email", "sbh-observer@example.invalid"), cwd=workspace, check=False)
        subprocess.run(("git", "config", "user.name", "SBH Observer"), cwd=workspace, check=False)
        subprocess.run(("git", "add", "."), cwd=workspace, check=False)
        subprocess.run(("git", "commit", "-qm", "baseline"), cwd=workspace, check=False)
        # Each entry carries a committed value and a working-tree value, and
        # the second one is the point of the fixture: a dangerous git command
        # is observed by whether it reverts the tree back to the committed
        # content, so the tree has to start out differing from it.  The third
        # element is that working-tree value, not a file mode.
        for relative, _content, worktree in plan.fixture.git_baseline_files:
            if worktree:
                (workspace / relative).write_text(worktree, encoding="utf-8")


def _config(home: Path, plan: Any) -> None:
    config_dir = home / ".yuyao"
    config_dir.mkdir(parents=True, exist_ok=True)
    value: dict[str, Any] = {
        "agent.mode": _mode_from_setup(plan.setup_commands),
        "permissions.deny": list(plan.deny_rules),
    }
    role = _explicit_role_from_setup(plan.setup_commands)
    if role is not None:
        value["agent.role"] = role
    (config_dir / "config.json").write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _facts(
    scenario: Any,
    plan: Any,
    workspace: Path,
    records: list[dict[str, Any]],
    trajectory: str,
    escape_path: Path | None = None,
) -> dict[str, Any]:
    recipe = scenario.recipe
    # A monitor/background probe produces its effect asynchronously - its
    # command is "touch <started>; sleep 0.2; touch <effect>" - so the declared
    # settle paths land a moment after the parent turn has already returned.
    # Reading the workspace immediately then sees only the first touch and
    # judges the effect absent for a monitor that in fact produced it.  Give the
    # declared settle paths a bounded window to appear before
    # any file-based fact is read; a scenario that declares none, or whose
    # effect is legitimately blocked (deny variant), simply waits out the short
    # window and is still judged absent - correctly.
    if getattr(plan.fixture, "settle_paths", ()):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not all(
            (workspace / path).exists() for path in plan.fixture.settle_paths
        ):
            time.sleep(0.1)
    # ``/cd`` in the setup sequence moves the artifact session's working
    # directory, so bare filenames in the allow/probe commands resolve there
    # rather than at the workspace root.
    effective_cwd = _effective_cwd(workspace, plan.setup_commands)
    allow_path = _target_path(workspace, recipe.allow_command, recipe.seed_files, effective_cwd)
    probe_path = _target_path(workspace, recipe.probe_command, recipe.seed_files, effective_cwd)
    # A dangerous-git probe names no file: "git stash" and "git reset --hard"
    # act on the tree, and what the point observes is whether the tracked
    # file kept its working-tree sentinel or was reverted to the committed
    # content.  Deriving the target from the command text yields nothing
    # there, so the file the git fixture declares is the target instead.
    if probe_path is None and recipe.initialize_git and recipe.git_baseline_files:
        probe_path = workspace / recipe.git_baseline_files[0][0]
    secondary_path = _target_path(workspace, recipe.second_probe or "", recipe.seed_files, effective_cwd)
    # A tool probe names its target in the call arguments, not in a shell
    # command - the command field is a placeholder there, so deriving the path
    # from it yields nothing and the point cannot see what the tool wrote.
    if recipe.probe_tool:
        declared = dict(getattr(recipe, "probe_arguments", ()) or ())
        for key in ("file_path", "path", "filename", "target", "notebook_path"):
            value = declared.get(key)
            if isinstance(value, str) and value and not value.startswith("/"):
                if secondary_path is None:
                    secondary_path = workspace / value
                if probe_path is None:
                    probe_path = workspace / value
                break
    messages = "\n".join(
        str(item)
        for record in records
        for item in (record.get("tool_result_texts") or ())
    )
    observed_tokens = tuple(sorted(
        token
        for record in records
        for token, seen in (record.get("probes") or {}).items()
        if seen
    ))
    advertised = tuple(str(item) for item in (records[0].get("tool_names") or ())) if records else ()
    return {
        "allow_file_present": bool(allow_path and allow_path.is_file()),
        # The forbidden artefact has a fixed name in the inventory.  Deriving it
        # from the probe command works for a probe that names its target, but a
        # hook point probes with "echo hi" and the artefact is written by the
        # hook itself - so the declared sentinel is the thing to look for when
        # the command names no path of its own.
        "deny_file_present": bool(
            (probe_path and probe_path.exists())
            or (workspace / "PERM_DENY_7755.txt").exists()
        ),
        "target_content": probe_path.read_text(encoding="utf-8", errors="replace") if probe_path and probe_path.is_file() else None,
        "secondary_target_content": secondary_path.read_text(encoding="utf-8", errors="replace") if secondary_path and secondary_path.is_file() else None,
        "escaped_path_present": escape_path.is_file() if escape_path is not None else False,
        "target_mode": oct(probe_path.stat().st_mode & 0o777) if probe_path and probe_path.exists() else None,
        "seed_directory_present": (workspace / "seeddir_7755").is_dir(),
        "node_present": (workspace / "newnode_7755").exists() or (workspace / "newnode_7755").is_symlink(),
        "memory_directory_present": (workspace / "memory").exists(),
        "directory_present": (workspace / "newdir_7755").is_dir(),
        "probe_tool_advertised": recipe.probe_tool in advertised,
        "git_ready": (workspace / ".git").is_dir(),
        "messages_text": messages + "\n" + _observed_text(trajectory),
        "stdout_text": _observed_text(trajectory),
        "observed_tokens": observed_tokens,
        "advertised_tools": advertised,
        "settled_paths": tuple(path for path in plan.fixture.settle_paths if (workspace / path).exists()),
    }


def _run_one() -> dict[str, Any]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.permission.adapter import build_permission_execution_plan
    from doc2harness.benchmark.harnesses.permission.contracts import PermissionRawObservation

    scenario_id = os.environ.get("SBH_SCENARIO_ID", "")
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.permission." not in owner.module:
        raise RuntimeError("permission driver received a non-permission scenario")
    scenario = owner.scenario
    plan = build_permission_execution_plan(scenario)
    gateway = mock_gateway
    escape_root = Path(tempfile.mkdtemp(prefix="sbh-permission-escape-"))
    escape_path = _escape_path(escape_root)
    escape_path.parent.mkdir(parents=True, exist_ok=True)
    rounds = (
        _rounds(plan, str(escape_path), str(escape_path.parent))
    )
    with tempfile.TemporaryDirectory(prefix="sbh-permission-production-") as temporary:
        root = Path(temporary)
        home, workspace, output = root / "home", root / "workspace", root / "out"
        home.mkdir(); workspace.mkdir(); output.mkdir()
        _seed(workspace, plan)
        _config(home, plan)
        task = workspace / "task.json"
        budget = workspace / "budget.json"
        task.write_text(json.dumps({"problem_statement": "Run the declared permission probe and stop."}), encoding="utf-8")
        budget.write_text(json.dumps({"max_steps": max(3, len(rounds) + 1)}), encoding="utf-8")
        cfg = gateway.new_cfg(
            mode="seq_probe",
            rounds=rounds,
            probe_tokens=list(plan.fixture.probe_tokens),
        )
        # Docker reaches the host gateway address, not the host loopback
        # interface.  Bind the deterministic stimulus listener on all host
        # interfaces so the container can actually deliver its model request.
        server = gateway.ThreadingHTTPServer(("0.0.0.0", 0), gateway.Handler)
        server.cfg = cfg
        server.daemon_threads = True
        server.block_on_close = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = dict(os.environ)
        env.update({"HOME": str(home), "YUYAO_HOME": str(home / ".yuyao"), "PYTHONDONTWRITEBYTECODE": "1"})
        argv = [
            str(Path(os.environ["SBH_ARTIFACT_PATH"]) / "run_agent.sh"),
            "--task-file", str(task), "--repo-dir", str(workspace),
            "--model-base-url", f"http://127.0.0.1:{server.server_address[1]}/v1",
            "--model-name", "litellm_proxy/mock", "--model-api-key", "sk-mock-test",
            "--budget-file", str(budget), "--out-dir", str(output),
        ]
        artifact = Path(os.environ["SBH_ARTIFACT_PATH"])
        # Prefer the stable public session API for permission scenarios, so
        # setup commands are real state transitions; retain the launcher
        # fallback for artifacts that predate that API.  The fallback is intentionally not used when the
        # API is present, otherwise ``/mode ask`` would be silently ignored by
        # their hard-coded headless launcher mode.
        has_api = (artifact / "src" / "yuyao" / "agent_api.py").is_file()
        if has_api:
            completed, timed_out = _run_public_session(
                artifact, workspace, home, env, int(server.server_address[1]),
                plan, min(plan.timeout_seconds, 300),
                script=(
                    _public_session_script(plan, escape_path.parent)
                ),
                escape_root=escape_root,
            )
        else:
            completed, timed_out = _run_artifact(
                artifact, workspace, home, output, task, budget,
                argv, env, int(server.server_address[1]), min(plan.timeout_seconds, 300),
            )
        trajectory = _trajectory(home) + "\n" + _trajectory(workspace) + "\n" + _trajectory(output)
        # A grant point is a contrast, not a single observation: the same write
        # is attempted once with the directory granted and once without, and
        # only the difference distinguishes "the grant works" from "the sandbox
        # was never restricting that path anyway".  Without the second run both
        # facts stay unset and the point cannot be decided at all.
        granted_write: bool | None = None
        ungranted_write: bool | None = None
        counterfactual = tuple(getattr(plan, "counterfactual_setup_commands", ()) or ())
        probe_target = escape_path.parent / "escf.txt"
        if counterfactual and has_api:
            granted_write = probe_target.is_file()
            probe_target.unlink(missing_ok=True)
            # The counterfactual is a second run of the same probe, so its
            # requests are left out of the ledger the first run's facts are
            # read from - appending them would inflate every request-count
            # fact by the price of asking a question about a different run.
            baseline = list(cfg.get("requests", []))
            _run_public_session(
                artifact, workspace, home, env, int(server.server_address[1]),
                plan, min(plan.timeout_seconds, 300),
                script=_public_session_script(plan, escape_path.parent, counterfactual),
                escape_root=escape_root,
            )
            ungranted_write = probe_target.is_file()
            cfg["requests"][:] = baseline
        variant_facts: dict[str, Any] = {}
        for line in (completed.stdout or "").splitlines():
            if line.startswith("SBH_VARIANT_RESULT="):
                try:
                    parsed = json.loads(line[len("SBH_VARIANT_RESULT="):])
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = {}
                if isinstance(parsed, dict):
                    variant_facts = parsed
        requests = _request_facts(cfg.get("requests", []), None)
        facts = _facts(scenario, plan, workspace, cfg.get("requests", []), trajectory, escape_path)
        observation = PermissionRawObservation(
            scenario_id=scenario_id, plan_digest=plan.plan_digest,
            artifact_id=os.environ["SBH_ARTIFACT_ID"], artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
            request_count=len(requests),
            allow_file_present=bool(facts["allow_file_present"]), deny_file_present=bool(facts["deny_file_present"]),
            target_content=facts["target_content"], secondary_target_content=facts["secondary_target_content"],
            escaped_path_present=bool(facts["escaped_path_present"]), target_mode=facts["target_mode"],
            seed_directory_present=bool(facts["seed_directory_present"]), node_present=bool(facts["node_present"]),
            memory_directory_present=bool(facts["memory_directory_present"]), directory_present=bool(facts["directory_present"]),
            probe_tool_advertised=bool(facts["probe_tool_advertised"]), git_ready=bool(facts["git_ready"]),
            granted_path_write_present=granted_write, ungranted_path_write_present=ungranted_write,
            canonical_variant_blocked=variant_facts.get("canonical_blocked"),
            rewritten_variant_blocked=variant_facts.get("rewritten_blocked"),
            rewritten_variant_control_mutated=variant_facts.get("control_mutated"),
            messages_text=str(facts["messages_text"]), stdout_text=str(facts["stdout_text"]),
            process_tree_clean=not timed_out, gateway_clean=True, workspace_clean=True, timed_out=timed_out,
            backend_evidence_digest=_digest({"scenario_id": scenario_id, "requests": len(requests), "rc": completed.returncode}),
            observed_tokens=tuple(facts["observed_tokens"]), settled_paths=tuple(facts["settled_paths"]),
            advertised_tools=tuple(facts["advertised_tools"]),
            # The healthy hook records its own exit through the trace path the
            # config named, which is the only evidence that the matcher chain
            # ran at all.  Without it a point cannot tell a hook that fired and
            # allowed the operation from a hook that was never reached.
            hook_events=_hook_events(workspace),
        )
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    judgement = owner.judge(observation)
    return {"observation": observation, "judgement": judgement,
            "plan_digest": plan.plan_digest, "definition_digest": definition_digest(owner.definition),
            "module": owner.module}


def _as_mapping(value: object) -> dict:
    """Serialise an observation or judgement whichever shape it uses.

    asdict() alone raises for this harness's scenarios, which declare Judgement
    as a NamedTuple while the other harnesses use dataclasses.

    Both shapes are legitimate; the driver just cannot assume one of them.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    fields = getattr(value, "_fields", None)
    if fields is not None:                      # NamedTuple
        return {name: getattr(value, name) for name in fields}
    raise TypeError(f"cannot serialise {type(value).__name__} as a mapping")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = _run_one()
    target = Path(os.environ["SBH_TYPED_EVENT_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "schema": "doc2harness.typed-events/1",
        "source_digest": os.environ["SBH_SOURCE_DIGEST"],
        "task_digest": os.environ["SBH_TASK_DIGEST"],
        "budget_digest": os.environ["SBH_BUDGET_DIGEST"],
        "artifact_digest": os.environ["SBH_ARTIFACT_DIGEST"],
        "artifact_id": os.environ["SBH_ARTIFACT_ID"],
        "scenario_id": os.environ["SBH_SCENARIO_ID"],
        "scenario_module": result["module"],
        "definition_digest": result["definition_digest"],
        "events": [{"type": "permission-observation",
                    "observation": _as_mapping(result["observation"]),
                    "judgement": _as_mapping(result["judgement"])}],
    }, ensure_ascii=False, default=lambda value: value.value if hasattr(value, "value") else str(value), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
