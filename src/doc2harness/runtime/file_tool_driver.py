"""Run one ``file`` scenario against an immutable artifact.

The driver deliberately keeps the observation surface small: the file-tool
scenario supplies fixtures and typed tool rounds, while the observer records
only request, lifecycle, and workspace facts.  It never imports recorded
result rows or turns model prose into a verdict.
"""

from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any
from doc2harness.runtime import mock_gateway
from doc2harness.runtime.direct_mock_driver import _digest, _run_artifact, _trajectory
from doc2harness.runtime.resource_limits import container_memory_flags


def _unfreeze(value: object) -> object:
    if isinstance(value, tuple):
        if all((isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in value)):
            return {str(key): _unfreeze(item) for key, item in value}
        return [_unfreeze(item) for item in value]
    return value


def _rounds(plan: Any) -> list[list[dict[str, Any]]]:
    rounds: list[list[dict[str, Any]]] = []
    for turn in plan.gateway_turns:
        calls: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            names: object = list(call.tool_names) if len(call.tool_names) > 1 else call.tool_names[0]
            arguments = dict(_unfreeze(call.arguments))
            logical = {str(item).lower().replace("-", "_") for item in call.tool_names}
            if logical & {"bash", "shell", "run_command", "execute", "exec", "terminal"}:
                calls.append({"name": names, "command": str(arguments.pop("command", "")), **arguments})
            else:
                calls.append({"name": names, "args": arguments})
        if calls:
            rounds.append(calls)
    return rounds


def _phase_rounds(raw: Any) -> list[list[dict[str, Any]]]:
    """Convert one declared phase into deterministic gateway rounds."""
    rounds: list[list[dict[str, Any]]] = []
    for turn in _unfreeze(raw) or []:
        calls: list[dict[str, Any]] = []
        for call in turn:
            names = list(call[0]) if isinstance(call[0], (list, tuple)) else [call[0]]
            arguments = dict(call[1]) if isinstance(call[1], (list, tuple, dict)) else {}
            label: Any = names if len(names) > 1 else names[0]
            logical = {str(item).lower().replace("-", "_") for item in names}
            if logical & {"bash", "shell", "run_command", "execute", "exec", "terminal"}:
                calls.append({"name": label, "command": str(arguments.pop("command", "")), **arguments})
            else:
                calls.append({"name": label, "args": arguments})
        if calls:
            rounds.append(calls)
    return rounds


def _phase_plan(plan: Any) -> dict[str, Any] | None:
    """Return the declared phase orchestration, or None when absent.

    An undo/redo point is not decided by the final file: it asserts a
    sequence - edited, reverted, re-applied - and each of those states is a
    separate snapshot the judge compares by name.  A single round cannot
    express that, which is why the recipe declares the phases and the slash
    command that separates them.
    """
    parameters = dict(plan.execution_parameters)
    if not parameters.get("phase1_rounds"):
        return None
    return {
        "phase1": _phase_rounds(parameters.get("phase1_rounds")),
        "phase2": _phase_rounds(parameters.get("phase2_rounds")),
        "phase3": _phase_rounds(parameters.get("phase3_rounds")),
        "mid": [str(item) for item in _unfreeze(parameters.get("mid_slash")) or []],
        "mid2": [str(item) for item in _unfreeze(parameters.get("mid_slash2")) or []],
    }


def _phase_session_script(plan: Any, phases: dict[str, Any]) -> str:
    """Drive the declared phases through the artifact public session API.

    The gateway hands out one declared round per model request, so the
    phases are simply consecutive chats against the same session, with the
    separating slash command dispatched in between.  A snapshot is taken
    after each boundary under the name the judge compares by.
    """
    parameters = dict(plan.execution_parameters)
    deny = [str(item) for item in _unfreeze(parameters.get("deny")) or []]
    setup = [str(item) for item in plan.setup_commands]
    targets = sorted({str(path) for path, _ in plan.fixture.seed_files} | {"tgt.txt"})
    body = [
        "import json, os",
        "from yuyao.agent_api import create_session",
        f"targets = {targets!r}",
        f"setup = {setup!r}",
        f"mid = {phases['mid']!r}",
        f"mid2 = {phases['mid2']!r}",
        f"phase2_len = {len(phases['phase2'])!r}",
        f"phase3_len = {len(phases['phase3'])!r}",
        "snapshots = {}",
        "def blob(session):",
        "    parts = []",
        "    for source in (getattr(session, 'executor', None), session):",
        "        try:",
        "            for message in (getattr(source, 'messages', None) or []):",
        "                content = message.get('content') if isinstance(message, dict) else None",
        "                if content:",
        "                    parts.append(str(content))",
        "        except BaseException:",
        "            pass",
        "    return chr(10).join(parts)",
        "def snap(label):",
        "    snapshots[label + 'model_text'] = blob(session)",
        "    root = str(getattr(session, 'cwd', None) or '/sbh-workspace')",
        "    for name in targets:",
        "        path = os.path.join(root, name)",
        "        stem = os.path.splitext(os.path.basename(name))[0]",
        "        try:",
        "            handle = open(path, encoding='utf-8', errors='replace')",
        "            value = handle.read()",
        "            handle.close()",
        "        except OSError:",
        "            value = None",
        "        snapshots[label + stem if label else stem] = value",
        f"session = create_session(deny={deny!r}, cwd='/sbh-workspace')",
        "prompt = 'Please run the declared tool calls now.'",
        "try:",
        "    for command in setup:",
        "        session.dispatch(command)",
        "    session.chat(prompt)",
        "    snap('phase1_')",
        "    for command in mid:",
        "        session.dispatch(command)",
        "    snap('after_mid1_')",
        "    if phase2_len:",
        "        session.chat(prompt)",
        "        snap('phase2_')",
        "    for command in mid2:",
        "        session.dispatch(command)",
        "    if mid2:",
        "        snap('after_mid2_')",
        "    if phase3_len:",
        "        session.chat(prompt)",
        "        snap('phase3_')",
        "    snap('')",
        "finally:",
        "    session.close()",
        "print('SBH_PHASE_RESULT=' + json.dumps(snapshots), flush=True)",
    ]
    return chr(10).join(body) + chr(10)


def _run_phase_session(
    artifact: Path,
    workspace: Path,
    home: Path,
    env: dict[str, str],
    port: int,
    script: str,
    timeout: int,
    cwd_mount: str = "/sbh-workspace",
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run a phase orchestration inside the evaluation image.

    ``cwd_mount`` is where the workspace is mounted and where the session runs.
    It defaults to ``/sbh-workspace`` (a top-level, read-only-parent path).  A
    ``..``-relative escape point cannot be exercised from there: its secret is
    seeded at ``/tmp/<f>`` but ``../<f>`` resolves to ``/<f>`` on the read-only
    root, so a product that fails to fence never reaches the secret and the
    point passes vacuously.  Such points pass ``/tmp/sbh-workspace`` so the
    workspace parent is the writable tmpfs the seed lands in and ``../<f>``
    reaches it - a non-fencing product then leaks/writes and fails, a fencing
    one refuses and passes.
    """
    image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
    command = [
        "docker",
        "run",
        *container_memory_flags(),
        "--rm",
        "--init",
        "--network",
        "bridge",
        "--add-host=host.docker.internal:host-gateway",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{artifact}:/sbh-artifact:ro",
        "-v",
        f"{workspace}:{cwd_mount}:rw",
        "-v",
        f"{home}:/sbh-home:rw",
        "-w",
        cwd_mount,
        "-e",
        "HOME=/sbh-home",
        "-e",
        "YUYAO_HOME=/sbh-home/.yuyao",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        f"D2A_MODEL_BASE_URL=http://host.docker.internal:{port}/v1",
        "-e",
        "D2A_MODEL_NAME=litellm_proxy/mock",
        "-e",
        "D2A_MODEL_API_KEY=sk-mock-test",
        "-e",
        "PYTHONPATH=/sbh-artifact/src",
        image,
        "python3",
        "-c",
        script,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (completed, False)
    except subprocess.TimeoutExpired as exc:
        return (subprocess.CompletedProcess(command, -1, exc.stdout or "", exc.stderr or ""), True)


def _wire_facts(requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise the structural properties of what reached the wire.

    A point about the message envelope - one leading system message, no
    orphan tool result, no dangling tool call - is decided by the requests
    themselves, not by any file.  The gateway already records these per
    request; the judge needs them folded across the whole exchange.
    """
    roles = [list(record.get("msg_roles") or []) for record in requests]
    return {
        "has_leading_system": any((item and item[0] == "system" for item in roles)),
        "nonleading_system": any((bool(record.get("nonleading_system")) for record in requests)),
        "had_tool_role": any(("tool" in item for item in roles)),
        "orphan_tool_result": any((bool(record.get("orphan_tool_result")) for record in requests)),
        "dangling_tool_use": any((bool(record.get("dangling_tool_use")) for record in requests)),
    }


def _apply_git_baseline(workspace: Path, plan: Any) -> bool:
    """Commit the declared baseline so auto-commit has a history to add to.

    An auto-commit point counts the commits the product created and reads
    the diff of the target against HEAD.  Without a repository there is no
    history to count and no HEAD to diff, so the point reports nothing
    rather than reporting what the product did.
    """
    declared = dict(_unfreeze(dict(plan.execution_parameters).get("git_baseline") or ()) or {})
    if not declared:
        return False
    identity = ("-c", "user.email=fixture@example.invalid", "-c", "user.name=SBH Fixture")

    def run(*arguments: str) -> None:
        subprocess.run(
            ("git", *identity, *arguments),
            cwd=workspace,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    for relative, content in dict(declared.get("commit") or {}).items():
        path = workspace / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
    run("init", "-q")
    run("config", "user.email", "fixture@example.invalid")
    run("config", "user.name", "SBH Fixture")
    run("add", "-A")
    run("commit", "-q", "-m", "baseline", "--allow-empty")
    for relative, content in dict(declared.get("worktree") or {}).items():
        (workspace / str(relative)).write_text(str(content), encoding="utf-8")
    return True


def _git_facts(workspace: Path, plan: Any) -> dict[str, Any]:
    """Count commits and read the target diff the auto-commit judges use."""
    if not (workspace / ".git").is_dir():
        return {}
    targets = [str(path) for path, _ in plan.fixture.seed_files] or ["tgt.txt"]
    log = subprocess.run(
        ("git", "log", "--oneline"), cwd=workspace, check=False, capture_output=True, text=True
    )
    diff = subprocess.run(
        ("git", "diff", "HEAD", "--", targets[0]), cwd=workspace, check=False, capture_output=True, text=True
    )
    tracked = subprocess.run(
        ("git", "ls-files", "-z", "--", targets[0]), cwd=workspace, check=False, capture_output=True
    )
    matches = tuple(
        sorted((item.decode("utf-8", "replace") for item in (tracked.stdout or b"").split(b"\x00") if item))
    )
    return {
        "git_ok": True,
        "git_tree_observer_ok": log.returncode == 0 and tracked.returncode == 0,
        "git_pathspec_matches": matches,
        "git_commit_count": len([x for x in (log.stdout or "").splitlines() if x.strip()]),
        "git_diff_target": str(diff.stdout or ""),
    }


def _mode_session_script(plan: Any, cwd: str = "/sbh-workspace") -> str:
    """Drive the declared rounds through a session with the declared mode.

    The artifact launcher hard-codes yolo (entry.py), so a point whose
    whole subject is a restricted mode ran unrestricted through it: the
    mutating tools stayed advertised and the write landed, which reads as
    "read-only failed to block" when read-only was never in effect.

    yolo is therefore the default *only for a point that declares no
    restriction* - a full-permission point that needs bash and writes
    auto-approved on every product.  A point that declares a restriction
    (``/mode X``, ``/ask``, ``/plan``, or ``/role ask``) must run under it,
    because the permission layer gates by mode alone (role only tunes the
    system prompt).  Blanket-forcing yolo onto a ``/role ask`` point re-opens
    the very write it exists to block: the mutating call the mock issues is
    auto-approved, the target changes, and a genuinely restrictive default
    ("ask") reads as "failed to block" - a false FAIL the driver invented.
    A pure ``/role ask`` keeps the product's own default mode (ask, per
    §3.1) rather than being pinned, so the restriction under test is the
    product's, not the harness's.
    """
    explicit_mode: str | None = None
    for command in plan.setup_commands:
        text = str(command).strip()
        if "/mode " in text:
            explicit_mode = text.split("/mode ", 1)[1].split()[0]
        elif text == "/plan":
            explicit_mode = explicit_mode or "plan"
        elif text == "/ask":
            explicit_mode = explicit_mode or "ask"
    role = (
        "ask"
        if any(("/role ask" in str(c) or str(c).strip() == "/ask" for c in plan.setup_commands))
        else None
    )
    if explicit_mode is not None:
        mode: str | None = explicit_mode
    elif role == "ask":
        mode = None
    else:
        mode = "yolo"
    deny = [str(item) for item in _unfreeze(dict(plan.execution_parameters).get("deny")) or []]
    mode_argument = f"mode={mode!r}, " if mode is not None else ""
    role_argument = f"role={role!r}, " if role else ""
    return f"import json\nfrom yuyao.agent_api import create_session\nsession = create_session({mode_argument}{role_argument}deny={deny!r}, cwd={cwd!r})\ntry:\n    session.chat('Please run the declared tool calls now.')\nfinally:\n    session.close()\n"


def _config(home: Path, plan: Any) -> None:
    config_root = home / ".yuyao"
    config_root.mkdir(parents=True, exist_ok=True)
    mode = "yolo"
    for command in plan.setup_commands:
        text = str(command)
        if "/mode " in text:
            mode = text.split("/mode ", 1)[1].split()[0]
    value: dict[str, object] = {"agent.mode": mode}
    if dict(plan.execution_parameters).get("enable_autocommit"):
        value["autoCommit"] = True
    (config_root / "config.json").write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _path_values(plan: Any) -> list[str]:
    paths: list[str] = [str(path) for path, _ in plan.fixture.seed_files]
    for turn in plan.gateway_turns:
        for call in turn.tool_calls:
            args = dict(_unfreeze(call.arguments))
            for key in (
                "path",
                "file_path",
                "filepath",
                "filename",
                "target",
                "target_file",
                "notebook_path",
            ):
                value = args.get(key)
                if isinstance(value, str) and value and (not value.startswith("/")):
                    paths.append(value)
    return list(dict.fromkeys(paths))


def _facts(workspace: Path, plan: Any) -> tuple[str | None, str | None, bool, tuple[tuple[str, object], ...]]:
    paths = _path_values(plan)
    candidates = [path for path in paths if path not in {"live.txt", "", "."}]
    existing = [workspace / path for path in candidates if (workspace / path).is_file()]
    target = existing[0].read_text(encoding="utf-8", errors="replace") if existing else None
    secondary = existing[-1].read_text(encoding="utf-8", errors="replace") if existing else None
    live = (workspace / "live.txt").is_file()
    files = tuple(
        sorted(
            (
                (path, (workspace / path).read_text(encoding="utf-8", errors="replace"))
                for path in candidates
                if (workspace / path).is_file()
            )
        )
    )
    notebook = workspace / "nb.ipynb"
    if notebook.is_file():
        files = tuple(
            sorted((dict(files) | {"nb": notebook.read_text(encoding="utf-8", errors="replace")}).items())
        )
    return (target, secondary, live, files)


def _observed_model_text(trajectory: str) -> str:
    """Project the trajectory down to what the model and tools actually said.

    The scenarios assert that a forbidden marker never appears in the model's
    view.  Matching against the raw trajectory also matches the marker inside
    the recorded arguments of a tool call - that is the request the product then
    *refused*, so the assertion inverts: a product that correctly blocked a
    fence escape reads as having leaked it.  The judges look only at message
    content, never at call arguments, and the fixtures build their
    markers by concatenation precisely so the full string cannot appear in
    command text.

    Artifacts record trajectories in different shapes - one writes JSONL, one
    writes pretty-printed JSON interleaved with Markdown - so the argument
    payloads are removed textually rather than by parsing a specific schema.
    Tool results, replies and reasoning are all preserved, including the failure
    text that proves a refusal happened.

    The runner's own per-call bookkeeping is dropped for the same reason.  A
    recorded ``"meta": {"truncated": false, ...}`` is the runner describing the
    call to its log, not text any model read; a judge asking whether the
    product *claimed* truncation found the word inside that structure and
    reported a truncation claim on a result whose own text says nothing of the
    kind.  The tool's own output survives
    this because it is a sibling field, not part of the bookkeeping.
    """
    without_arguments = re.sub(
        '"(?:arguments|args|input|meta)"\\s*:\\s*"(?:[^"\\\\]|\\\\.)*"', '"arguments": ""', trajectory
    )
    without_arguments = _strip_balanced_argument_objects(without_arguments)
    without_arguments = re.sub(
        '("kind"\\s*:\\s*"permission"[^\\n]*?)"target"\\s*:\\s*"(?:[^"\\\\]|\\\\.)*"',
        '\\1"target": ""',
        without_arguments,
    )
    without_arguments = re.sub("(- tool `[A-Za-z0-9_.-]+`)\\s*\\{[^\\n]*", "\\1", without_arguments)
    return without_arguments


def _strip_balanced_argument_objects(text: str) -> str:
    """Replace ``"arguments": {...}`` values, however deeply they nest.

    A character-class regex cannot span a nested payload such as multi-edit's
    ``{"edits": [{...}, {...}]}``, which would leave the very marker the
    scenario forbids inside the text the judge inspects.
    """
    pattern = re.compile('"(?:arguments|args|input|meta)"\\s*:\\s*(?=[{\\[])')
    result: list[str] = []
    index = 0
    while True:
        match = pattern.search(text, index)
        if match is None:
            result.append(text[index:])
            return "".join(result)
        result.append(text[index : match.start()])
        result.append('"arguments": {}')
        cursor = match.end()
        depth = 0
        in_string = False
        escaped = False
        while cursor < len(text):
            char = text[cursor]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char in "{[":
                depth += 1
            elif char in "}]":
                depth -= 1
                if depth == 0:
                    cursor += 1
                    break
            cursor += 1
        index = cursor


def _run_one() -> dict[str, Any]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.file.adapter import build_file_tool_execution_plan
    from doc2harness.benchmark.harnesses.file.contracts import FileToolRawObservation

    scenario_id = os.environ.get("SBH_SCENARIO_ID", "")
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.file." not in owner.module:
        raise RuntimeError("file-tool driver received a non-file-tool scenario")
    scenario = owner.scenario
    plan = build_file_tool_execution_plan(scenario)
    gateway = mock_gateway
    with tempfile.TemporaryDirectory(prefix="sbh-file-tool-production-") as temporary:
        root = Path(temporary)
        home, workspace, output = (root / "home", root / "workspace", root / "out")
        home.mkdir()
        workspace.mkdir()
        output.mkdir()
        for relative, content in plan.fixture.seed_files:
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _apply_git_baseline(workspace, plan)
        _config(home, plan)
        task = workspace / "task.json"
        budget = workspace / "budget.json"
        task.write_text(
            json.dumps({"problem_statement": "Run the declared file-tool probe and stop."}), encoding="utf-8"
        )
        budget.write_text(json.dumps({"max_steps": max(3, len(plan.gateway_turns) + 1)}), encoding="utf-8")
        phases = _phase_plan(plan)
        if phases is not None:
            finish = [[{"name": "__finish__", "content": "done"}]]
            declared_rounds = []
            for name in ("phase1", "phase2", "phase3"):
                if phases[name]:
                    declared_rounds += phases[name] + finish
        else:
            declared_rounds = _rounds(plan)
        cfg = gateway.new_cfg(mode="seq_probe", rounds=declared_rounds)
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
            "--task-file",
            str(task),
            "--repo-dir",
            str(workspace),
            "--model-base-url",
            f"http://127.0.0.1:{server.server_address[1]}/v1",
            "--model-name",
            "litellm_proxy/mock",
            "--model-api-key",
            "sk-mock-test",
            "--budget-file",
            str(budget),
            "--out-dir",
            str(output),
        ]
        artifact_root = Path(os.environ["SBH_ARTIFACT_PATH"])
        phase_snapshots: dict[str, Any] = {}
        has_api = (artifact_root / "src" / "yuyao" / "agent_api.py").is_file()
        if phases is not None and has_api:
            completed, timed_out = _run_phase_session(
                artifact_root,
                workspace,
                home,
                env,
                int(server.server_address[1]),
                _phase_session_script(plan, phases),
                min(plan.timeout_seconds, 300),
            )
            for line in (completed.stdout or "").splitlines():
                if line.startswith("SBH_PHASE_RESULT="):
                    try:
                        parsed = json.loads(line[len("SBH_PHASE_RESULT=") :])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = {}
                    if isinstance(parsed, dict):
                        phase_snapshots = parsed
        elif has_api:
            cwd_mount = (
                "/sbh-workspace"
            )
            completed, timed_out = _run_phase_session(
                artifact_root,
                workspace,
                home,
                env,
                int(server.server_address[1]),
                _mode_session_script(plan, cwd_mount),
                min(plan.timeout_seconds, 300),
                cwd_mount=cwd_mount,
            )
        else:
            completed, timed_out = _run_artifact(
                artifact_root,
                workspace,
                home,
                output,
                task,
                budget,
                argv,
                env,
                int(server.server_address[1]),
                min(plan.timeout_seconds, 300),
            )
        target_content, secondary_content, live, files = _facts(workspace, plan)
        if phase_snapshots and "tgt" in phase_snapshots:
            target_content = phase_snapshots["tgt"]
        wire = _wire_facts(list(cfg.get("requests", [])))
        wire.update(_git_facts(workspace, plan))
        files = tuple(sorted((dict(files) | wire).items()))
        if phase_snapshots:
            merged = dict(files)
            merged.update({str(key): value for key, value in phase_snapshots.items() if value is not None})
            files = tuple(sorted(merged.items()))
        trajectory = _trajectory(home) + "\n" + _trajectory(workspace) + "\n" + _trajectory(output)
        requests = cfg.get("requests", [])
        result_texts = tuple(
            (str(text) for record in requests for text in record.get("tool_result_texts") or ())
        )
        files = tuple(sorted((dict(files) | {"target_tool_result_seen": bool(result_texts)}).items()))
        advertised = tuple((str(name) for name in requests[0].get("tool_names") or ())) if requests else ()
        observation = FileToolRawObservation(
            scenario_id=scenario_id,
            plan_digest=plan.plan_digest,
            artifact_id=os.environ["SBH_ARTIFACT_ID"],
            artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
            request_count=len(requests),
            advertised_tools=advertised,
            live=live,
            target_content=target_content,
            secondary_target_content=secondary_content,
            model_text=_observed_model_text(trajectory) + chr(10) + chr(10).join(result_texts),
            stderr_text=str(completed.stderr or ""),
            empty_tool_result=any((not text.strip() for text in result_texts)) if result_texts else None,
            process_tree_clean=not timed_out,
            gateway_clean=True,
            workspace_clean=True,
            timed_out=timed_out,
            backend_evidence_digest=_digest(
                {"scenario_id": scenario_id, "requests": len(requests), "rc": completed.returncode}
            ),
            target_tool_result=result_texts[-1] if result_texts else "",
            round_target_tool_results=result_texts,
            facts=files,
        )
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    judgement = owner.judge(observation)
    return {
        "observation": observation,
        "judgement": judgement,
        "plan_digest": plan.plan_digest,
        "definition_digest": definition_digest(owner.definition),
        "module": owner.module,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = _run_one()
    target = Path(os.environ["SBH_TYPED_EVENT_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema": "doc2harness.typed-events/1",
                "source_digest": os.environ["SBH_SOURCE_DIGEST"],
                "task_digest": os.environ["SBH_TASK_DIGEST"],
                "budget_digest": os.environ["SBH_BUDGET_DIGEST"],
                "artifact_digest": os.environ["SBH_ARTIFACT_DIGEST"],
                "artifact_id": os.environ["SBH_ARTIFACT_ID"],
                "scenario_id": os.environ["SBH_SCENARIO_ID"],
                "scenario_module": result["module"],
                "definition_digest": result["definition_digest"],
                "events": [
                    {
                        "type": "file-tool-observation",
                        "observation": asdict(result["observation"]),
                        "judgement": asdict(result["judgement"]),
                    }
                ],
            },
            ensure_ascii=False,
            default=lambda value: value.value if hasattr(value, "value") else str(value),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
