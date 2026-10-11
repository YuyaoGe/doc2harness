"""Run one ``headless`` scenario through the artifact's public CLI.

The driver deliberately observes process exit data, request records, and
workspace changes.  It never converts terminal prose into a typed fact used
by a judge; unsupported observations remain explicit in the observation.
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
from dataclasses import asdict
from pathlib import Path
from typing import Any

_TREE = "/sbh-tree"
# The container path of the mounted workspace.  It deliberately sits one level
# inside a writable parent; see the mount in _docker_run.
_WORKSPACE = _TREE + "/workspace"

from doc2harness.runtime import mock_gateway
from doc2harness.runtime.direct_mock_driver import _digest, _status
from doc2harness.runtime.resource_limits import container_memory_flags


def _snapshot(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if not path.is_file() or ".yuyao" in path.parts or ".trajectory" in path.parts:
            continue
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            result[path.relative_to(root).as_posix()] = path.read_bytes()
        except OSError:
            continue
    return result


def _system_probe_hits(record: dict[str, Any], probe_token: str = "") -> tuple[str, ...]:
    """Collect the tokens the gateway saw inside ``role=system`` messages.

    The gateway records these as ``probes_in_system`` (a token map) plus
    ``has_probe_in_system`` for the single configured ``probe_token``, which
    lives in the gateway config rather than in the record.  It deliberately
    keeps no request body, so there is no system text to read.  Reading a
    differently named key yields an empty system segment for every request,
    which the judges report as the product having dropped the flag.
    """
    hits = {
        str(key)
        for key, value in (record.get("probes_in_system") or {}).items()
        if value
    }
    if record.get("has_probe_in_system") and probe_token:
        hits.add(str(probe_token))
    return tuple(sorted(hits))


def _request_records(records: list[dict[str, Any]], probe_token: str = ""):
    from doc2harness.benchmark.harnesses.headless.contracts import RequestRecord

    result = []
    for record in records:
        body = record.get("body")
        if not isinstance(body, str):
            # The whole record would include the gateway's own bookkeeping,
            # including the probe table, so a judge asking whether a token is
            # absent from the request would always find it.  Serialise only the
            # fields that carry what was actually sent.
            body = json.dumps(
                {
                    key: value
                    for key, value in record.items()
                    if key in {"system_text", "messages", "msg_roles", "tool_names", "model"}
                },
                sort_keys=True,
                ensure_ascii=False,
            )
        probes = record.get("probes") or {}
        result.append(
            RequestRecord(
                body=body,
                model=str(record.get("model") or ""),
                system_text=str(record.get("system_text") or ""),
                probe_hits=tuple(str(k) for k, v in probes.items() if v),
                system_probe_hits=_system_probe_hits(record, probe_token),
                tool_names=tuple(str(v) for v in (record.get("tool_names") or ())),
                tool_result_texts=tuple(str(v) for v in (record.get("tool_result_texts") or ())),
                stream_enabled=record.get("stream") if isinstance(record.get("stream"), bool) else None,
            )
        )
    return tuple(result)


def _rounds(recipe: Any, observer: str = "") -> list[list[dict[str, Any]]]:
    rounds: list[list[dict[str, Any]]] = []
    for group in recipe.rounds:
        current: list[dict[str, Any]] = []
        for call in group.calls:
            item: dict[str, Any] = {"name": call.names[0]}
            if call.command:
                item["command"] = call.command
            try:
                arguments = json.loads(call.arguments_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                arguments = {}
            if isinstance(arguments, dict):
                # An address in a declared call can only name the observer once
                # the run has a bound listener, and the product reaches the host
                # under a different name than the host uses for itself.  Left
                # unresolved the fetch the point is about never arrives, and a
                # point asking whether the fetch was refused cannot tell that
                # from a fetch that was never attempted.
                if observer:
                    arguments = {
                        key: value.replace("127.0.0.1:__D2A_MOCK_PORT__", observer)
                        if isinstance(value, str) else value
                        for key, value in arguments.items()
                    }
                # The gateway reads a bash call's arguments from top-level
                # keys but every other tool's from "args"; spreading a grep
                # call's arguments top-level would drop them.
                if call.names[0] in {"bash", "shell", "run_command", "execute", "exec", "terminal"}:
                    item.update(arguments)
                else:
                    item["args"] = arguments
            current.append(item)
        if current:
            rounds.append(current)
    return rounds


def _json_object(pairs: list[tuple[str, Any]]) -> bytes:
    """Serialise pairs verbatim, keeping any repeated key.

    A duplicate-key contract probe cannot be produced by json.dumps, which
    silently collapses the repetition that is the whole subject of the
    point.  The unique variant is written the same way so both sides differ
    only in the repetition itself.
    """

    body = ",".join(
        json.dumps(key, ensure_ascii=False) + ":"
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for key, value in pairs
    )
    return ("{" + body + "}").encode()


def _formal_probe_count(recipe: Any) -> int:
    return sum(
        len(getattr(recipe, name, ()) or ())
        for name in (
            "formal_contract_probes", "formal_runtime_probes",
            "listing_probes", "cli_file_probes",
        )
    )


def _formal_rounds(recipe: Any) -> list[list[dict[str, Any]]]:
    """Rounds for one formal probe run, repeated once per declared probe.

    The two families differ in the exchange they specify.  A contract probe is
    a tool round plus the declared reply, which its judge reads as exactly two
    requests; a runtime-matrix probe is answered in one turn and its judge
    requires exactly one.  Sharing a single shape would make one of the two
    fail on the request count alone.
    """

    count = max(1, _formal_probe_count(recipe))
    if getattr(recipe, "formal_contract_probes", ()):
        turn = [
            [{"name": "bash", "command": "echo FORMAL_PROBE_ACK"}],
            [{"name": "__finish__", "content": recipe.reply_content}],
        ]
    else:
        turn = [[{"name": "__finish__", "content": recipe.reply_content}]]
    return turn * count


def _probe_by_label(recipe: Any, label: str) -> Any:
    """Find the declared probe a variant label came from, if any."""

    for name in ("formal_contract_probes", "formal_runtime_probes", "cli_file_probes"):
        for probe in getattr(recipe, name, ()) or ():
            if str(getattr(probe, "label", "")) == label:
                return probe
    return None


def _cli_file_variants(recipe: Any, workspace: Path) -> list[tuple[str, list[str]]]:
    """Build one module-CLI invocation per declared CLI-file probe.

    The probe declares the exact bytes of a file the product is handed through
    a user-facing flag, so the bytes are written verbatim - the point is about
    what happens at the decoding boundary, which a text round-trip would have
    already resolved.
    """

    variants: list[tuple[str, list[str]]] = []
    for probe in getattr(recipe, "cli_file_probes", ()) or ():
        label = str(probe.label)
        kind = str(getattr(probe, "kind", ""))
        if kind != "system-prompt":
            continue
        path = workspace / f"system-prompt-{label}.txt"
        path.write_bytes(bytes.fromhex(str(probe.file_hex)))
        variants.append((label, [
            "-p", str(recipe.task or "Return the fixed reply and stop."),
            "--output-format", "text",
            "--system-prompt-file", f"{_WORKSPACE}/{path.name}",
        ]))
    return variants


def _formal_probe_variants(
    recipe: Any, workspace: Path, output: Path, port: int,
) -> list[tuple[str, list[str]]]:
    """Build one launcher invocation per declared formal probe."""

    probes = list(getattr(recipe, "formal_contract_probes", ()) or ())
    probes += list(getattr(recipe, "formal_runtime_probes", ()) or ())
    # A listing probe is not a launcher invocation: it seeds sessions through
    # the session API and samples process counters around the listing, so it
    # is driven by its own two-phase child rather than by run_agent.sh.  A
    # CLI-file probe is not one either: it hands the product a file through a
    # user-facing flag, and that route deliberately writes no status document -
    # which is exactly the "MISSING" status the point requires.  The launcher
    # would produce a status document the route it is about never writes.
    if not probes:
        return []
    variants: list[tuple[str, list[str]]] = []
    for probe in probes:
        label = str(probe.label)
        task_file = workspace / f"task-{label}.json"
        budget_file = workspace / f"budget-{label}.json"
        task: dict[str, Any] = {
            "task_id": label,
            "type": "swe",
            "problem_statement": str(recipe.task),
            "repo_dir": _WORKSPACE,
            "allow_network": False,
        }
        for override in getattr(probe, "task_overrides", ()) or ():
            task[str(override.field)] = override.canonical_value
        budget: dict[str, Any] = {"max_steps": 4, "max_tokens": 0, "max_wall_seconds": 30}
        for override in getattr(probe, "budget", ()) or ():
            raw = str(override.canonical_value)
            budget[str(override.field)] = int(raw) if raw.lstrip("-").isdigit() else raw
        contract = str(getattr(probe, "raw_contract", "") or "")
        task_pairs = list(task.items())
        budget_pairs: list[tuple[str, Any]] = list(budget.items())
        if contract == "duplicate-task":
            task_pairs = [
                ("task_id", task["task_id"]), ("type", task["type"]),
                ("problem_statement", "Return the first fixed reply."),
                ("problem_statement", "Return the second fixed reply."),
                ("repo_dir", task["repo_dir"]), ("allow_network", False),
            ]
        if contract == "duplicate-budget":
            budget_pairs = [
                ("max_steps", 4), ("max_steps", 5),
                ("max_tokens", 0), ("max_wall_seconds", 30),
            ]
        if contract in {"unique-task", "duplicate-task"}:
            task_file.write_bytes(_json_object(task_pairs))
        else:
            task_file.write_text(json.dumps(task, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if contract in {"unique-budget", "duplicate-budget"}:
            budget_file.write_bytes(_json_object(budget_pairs))
        else:
            budget_file.write_text(json.dumps(budget, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        values = {
            "--task-file": str(task_file).replace(str(workspace), _WORKSPACE),
            "--repo-dir": _WORKSPACE,
            "--model-base-url": f"http://host.docker.internal:{port}/v1",
            "--model-name": "litellm_proxy/mock",
            "--model-api-key": "sk-mock-test",
            "--budget-file": str(budget_file).replace(str(workspace), _WORKSPACE),
            "--out-dir": "/sbh-output",
        }
        # ``argv_overrides`` holds (flag, value) string pairs - not JsonOverride
        # objects with .field/.canonical_value.
        for flag_override, value_override in getattr(probe, "argv_overrides", ()) or ():
            values[str(flag_override)] = str(value_override)
        argv = ["--sbh-launcher"]
        for flag in ("--task-file", "--repo-dir", "--model-base-url", "--model-name",
                     "--model-api-key", "--budget-file", "--out-dir"):
            argv.extend((flag, values[flag]))
        variants.append((label, argv))
    return variants


_LISTING_PREFIX = "SBH-LISTING-ENTRY-"
_LISTING_PAYLOAD_END = "-SBH-LISTING-PAYLOAD-END-"
_LISTING_RESULT = "SBH_LISTING_RESULT="


def _listing_probe_script(phase: str, count: int, payload_bytes: int) -> str:
    """Build the child that seeds titled sessions, then lists them.

    The point asks whether listing sessions stays bounded as the stored
    transcripts grow: it compares read bytes, resident growth, opens under
    HOME and the printed size against the number of sessions seeded.  Those
    are facts about the product's own process, sampled around the dispatch,
    so they are gathered by a child inside the container - a driver watching
    from outside cannot attribute any of them to the listing.  Seeding and
    measuring are separate children on purpose: a measurement taken in the
    process that just wrote the corpus reads that writing, not the listing.
    """

    common = [
        "import contextlib, io, json, os, resource, sys",
        f"TAG = {_LISTING_RESULT!r}",
        f"PREFIX = {_LISTING_PREFIX!r}",
        f"PAYLOAD_END = {_LISTING_PAYLOAD_END!r}",
        f"COUNT = {int(count)}",
        f"PAYLOAD_BYTES = {int(payload_bytes)}",
        "def emit(value):",
        "    print(TAG + json.dumps(value, ensure_ascii=False, separators=(',', ':')))",
        "def close(session):",
        "    try:",
        "        session.close()",
        "    except BaseException:",
        "        pass",
    ]
    if phase == "seed":
        body = [
            "result = {'phase': 'seed', 'created': 0, 'product_error': '', 'reset_ok': True}",
            "session = None",
            "try:",
            "    from yuyao.agent_api import create_session",
            "    session = create_session(cwd=os.getcwd())",
            "    if not isinstance(session.messages, list) or not callable(getattr(session, 'dispatch', None)):",
            "        raise TypeError('standard AgentSession messages/dispatch unavailable')",
            "    for index in range(COUNT):",
            "        title = PREFIX + str(index).zfill(4)",
            "        answer = str(session.chat(title) or '')",
            "        if PAYLOAD_END not in answer or len(answer.encode('utf-8', 'replace')) != PAYLOAD_BYTES:",
            "            raise RuntimeError('mock assistant payload was not delivered exactly')",
            "        if PAYLOAD_END not in json.dumps(session.messages, ensure_ascii=False):",
            "            raise RuntimeError('assistant payload absent from public messages')",
            "        session.dispatch('/title ' + title)",
            "        session.dispatch('/new')",
            "        result['created'] += 1",
            "        if len(session.messages) != 0:",
            "            result['reset_ok'] = False",
            "            break",
            "except BaseException as exc:",
            "    result['product_error'] = type(exc).__name__ + ': ' + str(exc)[:240]",
            "finally:",
            "    close(session)",
            "emit(result)",
        ]
        return "\n".join(common + body) + "\n"
    if phase != "measure":
        raise ValueError(f"unknown listing probe phase {phase!r}")
    body = [
        "result = {'phase': 'measure', 'product_error': '', 'observer_error': ''}",
        "session = None",
        "def io_stats():",
        "    try:",
        "        data = {}",
        "        with open('/proc/self/io', 'r', encoding='ascii') as handle:",
        "            for line in handle:",
        "                key, _, value = line.partition(':')",
        "                data[key.strip()] = int(value.strip())",
        "        return data",
        "    except BaseException:",
        "        return None",
        "def rss_kib():",
        "    try:",
        "        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)",
        "    except BaseException:",
        "        return None",
        "try:",
        "    from yuyao.agent_api import create_session",
        "    session = create_session(cwd=os.getcwd())",
        "    if not callable(getattr(session, 'dispatch', None)):",
        "        raise TypeError('standard AgentSession dispatch unavailable')",
        "    opened = {'home': 0}",
        "    home = os.path.realpath(os.environ['HOME']) + os.sep",
        "    def audit(event, args):",
        "        if event != 'open' or not args:",
        "            return",
        "        try:",
        "            path = os.path.realpath(os.fsdecode(args[0]))",
        "        except BaseException:",
        "            return",
        "        if path.startswith(home):",
        "            opened['home'] += 1",
        "    sys.addaudithook(audit)",
        "    io_before, rss_before = io_stats(), rss_kib()",
        "    capture = io.StringIO()",
        "    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):",
        "        returned = session.dispatch('/sessions')",
        "    io_after, rss_after = io_stats(), rss_kib()",
        "    printed = capture.getvalue()",
        "    joiner = chr(10) if returned and printed else ''",
        "    public = (str(returned or '') + joiner + printed).strip()",
        "    absent = io_before is None or io_after is None or rss_before is None or rss_after is None",
        "    result.update({",
        "        'rchar_delta': None if io_before is None or io_after is None else max(0, io_after.get('rchar', 0) - io_before.get('rchar', 0)),",
        "        'read_bytes_delta': None if io_before is None or io_after is None else max(0, io_after.get('read_bytes', 0) - io_before.get('read_bytes', 0)),",
        "        'rss_delta_kib': None if rss_before is None or rss_after is None else max(0, rss_after - rss_before),",
        "        'home_open_count': opened['home'],",
        "        'output_bytes': len(public.encode('utf-8', 'replace')),",
        "        'listed_markers': public.count(PREFIX),",
        "    })",
        "    if absent:",
        "        result['observer_error'] = 'bytes/RSS observer unavailable'",
        "except BaseException as exc:",
        "    result['product_error'] = type(exc).__name__ + ': ' + str(exc)[:240]",
        "finally:",
        "    close(session)",
        "emit(result)",
    ]
    return "\n".join(common + body) + "\n"


def _listing_payload(payload_bytes: int) -> str:
    """The exact assistant reply the seed phase requires the mock to send."""

    suffix = len(_LISTING_PAYLOAD_END.encode("utf-8"))
    return "L" * max(0, int(payload_bytes) - suffix) + _LISTING_PAYLOAD_END


def _listing_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    """Read the child's declared result line out of its stdout."""

    parsed: dict[str, Any] | None = None
    for line in (completed.stdout or "").splitlines():
        if not line.startswith(_LISTING_RESULT):
            continue
        try:
            candidate = json.loads(line[len(_LISTING_RESULT):])
        except ValueError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
    return parsed


def _argv_variants(recipe: Any) -> list[tuple[str, list[str]]]:
    if recipe.sequence_subcommands:
        # Use the declared step names when the point has them: its judge looks
        # the steps up by name, and a positional "step-N" label makes every
        # lookup miss and the matrix read as incomplete.
        labels = list(getattr(recipe, "sequence_labels", ()) or ())
        return [
            (
                labels[index - 1]
                if index - 1 < len(labels)
                else f"step-{index}",
                list(arguments),
            )
            for index, arguments in enumerate(recipe.sequence_subcommands, start=1)
        ]
    base = ["-p", recipe.task, "--output-format", recipe.output_format]
    if recipe.append_system_prompt:
        base += ["--append-system-prompt", recipe.append_system_prompt]
    if recipe.system_prompt:
        base += ["--system-prompt", recipe.system_prompt]
    base += list(recipe.extra_argv)
    if recipe.subcommand_variants:
        return [(item.label, list(item.arguments)) for item in recipe.subcommand_variants]
    variants = recipe.cli_variants or recipe.file_variants
    if variants:
        return [(item.label, base + list(item.arguments)) for item in variants]
    return [("main", base)]


_STEP_MARKER = "SBH-STEP-BOUNDARY-"


def _sequence_script(steps: list[list[str]], settle_seconds: int) -> str:
    """Run every declared step in one shell, marking each boundary.

    A dispatch step starts a detached session and returns at once; the
    container's init reaps that session the moment the foreground process
    exits.  Running each step as its own container would kill the session
    before the next step could list or stop it.  One
    shell for the whole sequence keeps the session alive across the steps
    that are about it, and the declared window is honoured between them.
    """

    entry = "/sbh-artifact/src/yuyao/__main__.py"
    lines = ["AGENT_SESSION_ID="]
    for index, step in enumerate(steps, start=1):
        lines.append(f'echo "{_STEP_MARKER}{index}"')
        lines.append(
            "python3 " + entry + " "
            + " ".join(
                # A step naming the session an earlier step started cannot know
                # its identifier ahead of time, so the placeholder is resolved
                # in the shell that just saw the dispatch report it.
                '"$AGENT_SESSION_ID"' if item == "$AGENT_SESSION_ID" else shlex.quote(item)
                for item in step
            )
            + f" 2>&1 | tee /tmp/sbh-step-{index}.out"
        )
        lines.append(f'echo "{_STEP_MARKER}{index}-rc=${{PIPESTATUS[0]}}"')
        lines.append(
            "if [ -z \"$AGENT_SESSION_ID\" ]; then "
            f"AGENT_SESSION_ID=$(sed -n 's/^dispatched \\([A-Za-z0-9_-]*\\).*/\\1/p' /tmp/sbh-step-{index}.out | head -1); fi"
        )
        if settle_seconds:
            lines.append(f"sleep {int(settle_seconds)}")
    return "\n".join(lines) + "\n"


def _sequence_steps(text: str, count: int) -> list[tuple[str, int]]:
    """Split one sequence run's output back into per-step text and codes."""

    results: list[tuple[str, int]] = []
    for index in range(1, count + 1):
        start = text.find(f"{_STEP_MARKER}{index}\n")
        if start < 0:
            results.append(("", -1))
            continue
        start += len(f"{_STEP_MARKER}{index}\n")
        end = text.find(f"{_STEP_MARKER}{index}-rc=", start)
        body = text[start:end] if end >= 0 else text[start:]
        code = -1
        if end >= 0:
            tail = text[end + len(f"{_STEP_MARKER}{index}-rc="):].split("\n", 1)[0]
            if tail.strip().lstrip("-").isdigit():
                code = int(tail.strip())
        results.append((body, code))
    return results


_BOOKKEEPING_DIRS = {".git", ".yuyao", ".trajectory", "__pycache__", "node_modules"}


def _worktree_facts(
    workspace: Path, home: Path, target: str, needle: str,
) -> tuple[tuple[str, ...], str, str]:
    """Find the checkouts beside the repository and what they hold.

    A worktree point asserts two things at once: the write reached an
    isolated checkout, and the parent tree did not see it.  The second can be
    read from the workspace, but the first cannot - the checkout lives
    outside the repository, so looking only inside it finds nothing however
    well the product isolated the work, and the point reports the isolation
    absent.

    ``git worktree list`` answers in the paths the product saw, which are the
    container's.  They are translated back to the host so the content can
    actually be read; without that the paths are published and every read of
    them fails.
    """

    def host(path: str) -> Path:
        for container, local in ((_WORKSPACE, workspace), ("/sbh-home", home)):
            if path == container:
                return local
            if path.startswith(container + "/"):
                return local / path[len(container) + 1:]
        return Path(path)

    paths: list[str] = []
    listing = subprocess.run(
        ("git", "worktree", "list", "--porcelain"), cwd=workspace,
        capture_output=True, text=True, timeout=15, check=False,
    )
    for line in (listing.stdout or "").splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = line.split(" ", 1)[1]
        if host(candidate).resolve() != workspace.resolve():
            paths.append(candidate)
    # A product may build the checkout beside the repository without
    # registering it, so the declared sibling naming is checked too.  The
    # match is deliberately anchored on the repository's own name: a looser
    # one would collect the other variant's directory from the same parent.
    for sibling in sorted(workspace.parent.iterdir()):
        if not sibling.is_dir() or sibling.resolve() == workspace.resolve():
            continue
        if sibling.name.startswith(workspace.name + "-") or sibling.name.startswith(workspace.name + "__"):
            if str(sibling) not in paths:
                paths.append(str(sibling))
    name = Path(target).name if target else ""
    for item in paths:
        root = host(item)
        if not root.is_dir():
            continue
        candidates = [root / target] if target else []
        for base, directories, files in os.walk(root):
            directories[:] = [d for d in directories if d not in _BOOKKEEPING_DIRS]
            candidates.extend(Path(base) / found for found in files if found == name)
        for candidate in candidates:
            try:
                if not candidate.is_file() or candidate.stat().st_size > 200_000:
                    continue
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle and needle in content:
                return tuple(paths), content, str(candidate)
    return tuple(paths), "", ""


def _docker_command(
    artifact: Path,
    workspace: Path,
    home: Path,
    output: Path,
    argv: list[str],
    env: dict[str, str],
    port: int,
) -> list[str]:
    """Build the container invocation for one declared run."""

    image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
    command = [
        "docker", "run", *container_memory_flags(), "--rm", "--init", "--network", "bridge",
        "--add-host=host.docker.internal:host-gateway", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{artifact}:/sbh-artifact:ro",
        # The workspace is mounted inside a writable parent, not directly
        # under the read-only root, because a product that isolates a
        # background session builds its worktree as a *sibling* of the
        # repository.  With no writable directory beside it that sibling cannot
        # be created, the product silently falls back to the repository itself,
        # and a point about isolation observes the write landing exactly where
        # it asserted it would not.
        "-v", f"{workspace.parent}:{_TREE}:rw",
        # Without an explicit working directory the container starts in the
        # image default, so a relative path in a declared tool call lands
        # outside the mounted workspace: the write happens, the observer looks
        # in the right place and finds nothing, and the point reports that the
        # baseline write was never observed.
        "-w", _WORKSPACE,
        "-v", f"{home}:/sbh-home:rw",
        "-v", f"{output}:/sbh-output:rw",
    ]
    container_env = {
        "HOME": "/sbh-home",
        "YUYAO_HOME": "/sbh-home/.yuyao",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/sbh-artifact/src" + (":/sbh-artifact" if argv and argv[0] == "--sbh-python" else ""),
        "D2A_MODEL_BASE_URL": f"http://host.docker.internal:{port}/v1",
        "D2A_MODEL_NAME": "litellm_proxy/mock",
        "D2A_MODEL_API_KEY": "sk-mock-test",
    }
    # A recipe or probe may declare product settings of its own - retry limits,
    # for instance - and those are not addressed to the model connection, so
    # they are forwarded too.  The container's own paths still win: forwarding the host
    # HOME or YUYAO_HOME would point the product at directories it cannot see.
    declared = {
        key: value for key, value in env.items()
        if (key.startswith("D2A_") or key.startswith("MSWEA_"))
        and key not in container_env
    }
    for key, value in {**container_env, **declared}.items():
        command.extend(("-e", f"{key}={value}"))
    # A formal-contract probe drives the launcher with its seven declared
    # flags rather than the module CLI.  The sentinel lets one call site build
    # both without a second runner.
    if argv and argv[0] == "--sbh-launcher":
        command.extend((image, "bash", "/sbh-artifact/run_agent.sh"))
        command.extend(argv[1:])
    elif argv and argv[0] == "--sbh-python":
        # A listing probe drives the session API directly rather than the
        # module CLI, so it runs as its own interpreter inside the same
        # container: the counters it samples only mean something in the
        # process that performs the listing.
        command.extend((image, "python3", *argv[1:]))
    else:
        entry = "/sbh-artifact/src/yuyao/__main__.py"
        if argv and argv[0] == "--sbh-script":
            command.extend((image, "bash", "-lc", argv[1]))
        else:
            command.extend((image, "python3", entry))
            command.extend(argv)
    return command


def _docker_run(
    artifact: Path,
    workspace: Path,
    home: Path,
    output: Path,
    argv: list[str],
    env: dict[str, str],
    port: int,
    timeout: int,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    command = _docker_command(artifact, workspace, home, output, argv, env, port)
    try:
        return subprocess.run(command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout, check=False), False
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, -1, exc.stdout or "", exc.stderr or ""), True


def _run_one() -> dict[str, Any]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.headless.adapter import build_headless_execution_plan
    from doc2harness.benchmark.harnesses.headless.contracts import (
        HeadlessObservation, ObservedFile, ProcessObservation,
    )

    scenario_id = os.environ["SBH_SCENARIO_ID"]
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.headless." not in owner.module:
        raise RuntimeError("headless driver received a non-headless scenario")
    scenario = owner.scenario
    plan = build_headless_execution_plan(scenario)
    artifact = Path(os.environ["SBH_ARTIFACT_PATH"])
    gateway = mock_gateway
    server = gateway.ThreadingHTTPServer(("0.0.0.0", 0), gateway.Handler)
    recipe = scenario.recipe
    # A scenario that declares an appended system prompt but no explicit probe
    # token is asserting that the appended text reaches the wire, so the append
    # text *is* the probe.  Without this fallback the gateway is told to look
    # for nothing, every request records an empty system segment, and the judge
    # reports the product as having dropped the flag.
    probe_token = recipe.probe_token or getattr(recipe, "append_system_prompt", "") or ""
    probe_tokens = list(recipe.probe_tokens)
    if probe_token and probe_token not in probe_tokens:
        probe_tokens.append(probe_token)
    # A formal probe is answered by the declared fixed reply, not by the tool
    # sequence the other headless points drive: its judge compares the exact
    # reply text and a bounded request count, which a seq_probe round would
    # replace with a tool call and a different exchange length.  A CLI-file
    # probe declares its own gateway mode and is left to it: the formal shape
    # would replace its single fixed reply with a tool exchange and break the
    # request count the point pins.
    formal = bool(
        getattr(recipe, "formal_contract_probes", ())
        or getattr(recipe, "formal_runtime_probes", ())
        or getattr(recipe, "listing_probes", ())
    )
    cfg = gateway.new_cfg(
        mode="seq_probe" if formal else recipe.gateway_mode,
        reply_content=recipe.reply_content,
        probe_token=probe_token,
        probe_tokens=probe_tokens,
        command=recipe.command or "echo HEADLESS_PROOF_7731",
        # A verbose-route point names each model turn with its own token, so the
        # gateway is told which text to attach to the tool-call turn and which
        # to the reply.  The first and last declared probe tokens are those two
        # turns by construction.
        turn_contents=(
            None
        ),
        # The gateway hands out one declared round per request and the rounds
        # are shared across every variant in the run, so a formal point needs
        # its two-request exchange repeated once per probe - otherwise the
        # first probe consumes the sequence and the rest see an empty turn.
        rounds=(
            _formal_rounds(recipe)
            if formal
            # The gateway serves one declared round per request and the same
            # sequence covers every variant in the run, so a point that runs
            # several variants needs its rounds repeated once per variant -
            # otherwise the first variant consumes them and the rest observe an
            # exchange the recipe never described.  Each repetition also needs
            # its own closing turn: a variant's exchange does not end until the
            # model answers without a tool call, so without one the first
            # variant runs on into the next variant's round and the second is
            # handed the leftover reply instead of its own call.
            else (_rounds(
                recipe, f"host.docker.internal:{int(server.server_address[1])}"
            ) + [[{"name": "__finish__", "content": recipe.reply_content or "done"}]]
            ) * max(1, len(_argv_variants(recipe)))
        ),
    )
    server.cfg = cfg
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="sbh-headless-production-") as temporary:
        root = Path(temporary)
        home, workspace, output = root / "home", root / "tree" / "workspace", root / "out"
        # The workspace sits inside a directory of its own so the container can
        # mount that directory writable: a product isolating a background
        # session builds its worktree beside the repository, and it needs
        # somewhere to put it.  Only this tree is mounted, so home and output
        # stay where they are and are not exposed twice.
        workspace.parent.mkdir(parents=True)
        home.mkdir(); workspace.mkdir(); output.mkdir()
        for seed in recipe.seed_files:
            path = workspace / seed.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(seed.content, encoding="utf-8")
        # A worktree point is about a git operation not disturbing its parent
        # repository, so the workspace has to be one.  Without it the point can
        # only report that the fixture was unavailable.
        if any(variant.label == "wt" for variant in ((recipe.cli_variants or ()) + (recipe.file_variants or ()))):
            identity = ("-c", "user.email=fixture@example.invalid", "-c", "user.name=SBH Fixture")
            for arguments in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "baseline", "--allow-empty")):
                subprocess.run(
                    ("git", *identity, *arguments), cwd=workspace, check=False,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        env = dict(os.environ)
        env.update(dict(recipe.environment))
        env.update({"HOME": str(home), "YUYAO_HOME": str(home / ".yuyao"),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        runs: list[ProcessObservation] = []
        # A listing point is decided by facts about the product's own process -
        # bytes read, resident growth, opens under HOME - sampled around the
        # listing dispatch.  Each probe gets its own HOME so the corpus one
        # seeds is not counted against the other, and each is seeded and
        # measured by separate children so the measurement does not read the
        # writing that produced the corpus.
        listing_probes = tuple(getattr(recipe, "listing_probes", ()) or ())
        # A compact/rewind point is about session management across a
        # compaction, which only exists inside a live session: the checkpoint
        # the product offers, whether the pre-compact turn is still named, and
        # the listing's own selection syntax.
        session_facts: dict[str, Any] = {}
        for probe in listing_probes:
            probe_home = root / f"home-{probe.label}"
            probe_workspace = root / f"tree-{probe.label}" / "workspace"
            probe_home.mkdir(exist_ok=True)
            probe_workspace.mkdir(parents=True, exist_ok=True)
            probe_env = dict(env)
            probe_env.update({"HOME": str(probe_home), "YUYAO_HOME": str(probe_home / ".yuyao")})
            cfg["mode"] = "fixed_reply"
            cfg["reply_content"] = _listing_payload(probe.payload_bytes)
            script = probe_workspace / "sbh_listing_probe.py"
            row: dict[str, Any] = {
                "seed_created": 0, "listed_markers": None, "rchar_delta": None,
                "rss_delta_kib": None, "home_open_count": None, "output_bytes": None,
                "product_error": "", "start_error": "", "timed_out": False,
            }

            def run_phase(phase: str, timeout: int) -> tuple[dict[str, Any] | None, str]:
                script.write_text(
                    _listing_probe_script(phase, probe.count, probe.payload_bytes),
                    encoding="utf-8",
                )
                completed, expired = _docker_run(
                    artifact, probe_workspace, probe_home, output,
                    ["--sbh-python", f"{_WORKSPACE}/sbh_listing_probe.py"],
                    probe_env, int(server.server_address[1]), timeout,
                )
                if expired:
                    return None, "TIMEOUT"
                parsed = _listing_result(completed)
                if parsed is None:
                    tail = (completed.stderr or completed.stdout or "")[-160:]
                    return None, f"NO_RESULT_RC_{completed.returncode}:{tail}"
                return parsed, ""

            seeded, seed_error = run_phase("seed", int(recipe.seed_timeout_seconds or 120))
            if seed_error == "TIMEOUT":
                row["timed_out"] = True
            elif seed_error:
                row["start_error"] = f"SEED_{seed_error}"
            elif seeded is not None:
                row["seed_created"] = int(seeded.get("created") or 0)
                row["product_error"] = str(seeded.get("product_error") or "")
                if seeded.get("reset_ok") is not True and not row["product_error"]:
                    row["product_error"] = "public /new did not reset messages"
            if not row["start_error"] and not row["timed_out"]:
                measured, measure_error = run_phase(
                    "measure", int(recipe.measurement_timeout_seconds or 60)
                )
                if measure_error == "TIMEOUT":
                    row["timed_out"] = True
                elif measure_error:
                    row["start_error"] = f"MEASURE_{measure_error}"
                elif measured is not None:
                    for key in ("listed_markers", "rchar_delta", "rss_delta_kib",
                                "home_open_count", "output_bytes"):
                        row[key] = measured.get(key)
                    later = str(measured.get("product_error") or "")
                    if later:
                        row["product_error"] = (
                            f"{row['product_error']}; {later}" if row["product_error"] else later
                        )
            runs.append(ProcessObservation(
                label=probe.label,
                rc=0 if not row["start_error"] else -1,
                timed_out=bool(row["timed_out"]),
                start_error=str(row["start_error"]),
                cleanup_complete=not row["timed_out"],
                count=probe.count,
                payload_bytes=probe.payload_bytes,
                seed_created=row["seed_created"],
                listed_markers=row["listed_markers"],
                rchar_delta=row["rchar_delta"],
                rss_delta_kib=row["rss_delta_kib"],
                home_open_count=row["home_open_count"],
                output_bytes=row["output_bytes"],
                product_error=str(row["product_error"]),
            ))
        # A formal-contract or runtime-matrix point is specified over launcher
        # invocations with declared task/budget documents, not over the module
        # CLI flags the other points use.
        variants = [] if listing_probes or runs else (
            _cli_file_variants(recipe, workspace)
            or _formal_probe_variants(recipe, workspace, output, int(server.server_address[1]))
            or _argv_variants(recipe)
        )
        agent_session_id = ""
        # A sequence whose recipe declares a settle window is about work that
        # outlives the command that started it.  Run as separate containers,
        # that work would be reaped when its step exits, and the step that lists
        # or stops the session would find one that had already finished.  The
        # whole sequence therefore runs in one container: the session stays
        # alive across the steps that are about it.
        sequence_pause = min(int(recipe.sequence_pause_seconds or 0), 30)
        if sequence_pause and variants:
            steps = [list(argv) for _, argv in variants]
            # A point that acts on a session started by an earlier step needs
            # that session to still be running when it does.  The observer
            # answers instantly, so the session finishes first and "stopped a
            # dead session" reads exactly like "stopped a live one".  Holding
            # each answer for the declared settle window keeps it alive across
            # the steps that are about it.
            if len(steps) > 2:
                cfg["response_delay_s"] = sequence_pause * len(steps)
            script = _sequence_script(steps, sequence_pause)
            before = _snapshot(workspace)
            request_start = len(cfg.get("requests", []))
            completed, timed_out = _docker_run(
                artifact, workspace, home, output, ["--sbh-script", script], env,
                int(server.server_address[1]),
                min(recipe.timeout_seconds, 300) + sequence_pause * len(steps),
            )
            blob = str(completed.stdout or "") + "\n" + str(completed.stderr or "")
            found = re.search(
                r"dispatched\s+(?:background\s+session\s+)?([A-Za-z0-9_-]+)", blob, re.I
            )
            if found:
                agent_session_id = found.group(1)
            after = _snapshot(workspace)
            written = tuple(sorted(k for k in after if before.get(k) != after.get(k)))
            removed = tuple(sorted(k for k in before if k not in after))
            records = _request_records(
                list(cfg.get("requests", []))[request_start:], probe_token
            )
            outputs = _sequence_steps(str(completed.stdout or ""), len(steps))
            for (label, _), (text, code) in zip(variants, outputs):
                runs.append(ProcessObservation(
                    label=label,
                    rc=code,
                    stdout=text,
                    timed_out=timed_out,
                    requests=records,
                    written_paths=written,
                    removed_paths=removed,
                    exit_status=str(_status(output).get("exit_status") or ""),
                    cleanup_complete=not timed_out,
                    git_initialized=(workspace / ".git").is_dir(),
                ))
            variants = []
        for label, argv in variants:
            # A probe may name the protocol failure it is about and the
            # environment it needs - an upstream 401 with retries pinned to one
            # attempt, say.  Both are declared per probe, not per recipe; the
            # recipe-level mode alone would exercise the healthy exchange and
            # judge error reporting on a run where no error occurred.
            declared = _probe_by_label(recipe, label)
            probe_mode = str(getattr(declared, "gateway_mode", "") or "")
            probe_env = dict(getattr(declared, "environment", ()) or ())
            cfg["mode"] = probe_mode or ("seq_probe" if formal else recipe.gateway_mode)
            variant_env = {**env, **probe_env}
            # Variants are contrasted against each other - a permitted run
            # against a restricted one - so each has to start from the
            # declared fixture.  Carrying the previous variant's write over
            # makes the restricted run look like it performed a write it was
            # actually prevented from performing.
            for seed in recipe.seed_files:
                path = workspace / seed.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(seed.content, encoding="utf-8")
            request_start = len(cfg.get("requests", []))
            # The gets a variant made are sliced the same way its requests are:
            # the two flags share one observer, so an unsliced read would
            # attribute the permitted variant's fetch to the restricted one and
            # the point would report a leak that never happened.
            web_start = len(cfg.get("web_gets", []))
            before = _snapshot(workspace)
            # A step that acts on a session started by an earlier step has to
            # name that session, and its identifier only exists once the
            # dispatch has run.  The recipe writes the placeholder and it is
            # resolved from what the dispatch reported; left unresolved the
            # command is handed a literal "$AGENT_SESSION_ID" and stops the
            # session the point is about being unable to name.
            if agent_session_id:
                argv = [item.replace("$AGENT_SESSION_ID", agent_session_id) for item in argv]
            completed, timed_out = _docker_run(
                artifact, workspace, home, output, argv, variant_env,
                int(server.server_address[1]), min(recipe.timeout_seconds, 300),
            )
            if not agent_session_id:
                blob = str(completed.stdout or "") + "\n" + str(completed.stderr or "")
                found = re.search(
                    r"dispatched\s+(?:background\s+session\s+)?([A-Za-z0-9_-]+)", blob, re.I
                ) or re.search(
                    r"(?m)^\s*([A-Za-z0-9_-]{6,})\s+"
                    r"(?:running|waiting|completed|stopped|failed)\b",
                    blob, re.I,
                )
                if found:
                    agent_session_id = found.group(1)
            after = _snapshot(workspace)
            written = tuple(sorted(k for k in after if before.get(k) != after.get(k)))
            removed = tuple(sorted(k for k in before if k not in after))
            target = workspace / recipe.target_file if recipe.target_file else None
            target_content = target.read_text(encoding="utf-8", errors="replace") if target and target.is_file() else ""
            # An isolation point also has to see the checkout beside the
            # repository, which the workspace read cannot reach.
            worktree_paths, worktree_content, worktree_found = (
                _worktree_facts(workspace, home, recipe.target_file, recipe.target_written)
                if (workspace / ".git").is_dir() and recipe.target_file
                else ((), "", "")
            )
            status = _status(output)
            records = _request_records(
                list(cfg.get("requests", []))[request_start:], probe_token
            )
            runs.append(ProcessObservation(
                label=label,
                rc=int(completed.returncode),
                stdout=str(completed.stdout or ""),
                stderr=str(completed.stderr or ""),
                timed_out=timed_out,
                requests=records,
                written_paths=written,
                removed_paths=removed,
                target_content=target_content,
                worktree_paths=worktree_paths,
                worktree_target_content=worktree_content,
                marker_found_at=worktree_found,
                web_gets=tuple(
                    str(item.get("path") or "")
                    for item in list(cfg.get("web_gets", []))[web_start:]
                ),
                # A point can require that a route write *no* status document -
                # a user-facing CLI flag is not the unattended contract and
                # deliberately reports no terminal state.  Publishing an empty
                # string for that made "absent" indistinguishable from "present
                # but blank", so the absence is named.
                exit_status=str(status.get("exit_status") or ("" if status else "MISSING")),
                cleanup_complete=not timed_out,
                out_created=bool(status),
                repo_changed=bool(written or removed),
                git_initialized=(workspace / ".git").is_dir(),
                product_error=str(status.get("error") or ""),
            ))
        observed_files = tuple(
            ObservedFile(path, (workspace / path).read_text(encoding="utf-8", errors="replace"))
            for path in sorted({item.path for item in recipe.seed_files})
            if (workspace / path).is_file()
        )
        # Some points inspect what the artifact itself ships rather than what
        # the run produced - the README is the delivered document, not a seeded
        # fixture, so reading only the workspace leaves the judge with nothing
        # to inspect and it reports the file as missing.
        for shipped in ("README.md",):
            path = artifact / shipped
            if path.is_file():
                observed_files += (
                    ObservedFile(shipped, path.read_text(encoding="utf-8", errors="replace")),
                )
        observation = HeadlessObservation(
            scenario_id=scenario_id,
            profile=recipe.profile,
            plan_digest=plan.plan_digest,
            artifact_id=os.environ["SBH_ARTIFACT_ID"],
            artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
            backend_evidence_digest=_digest({"scenario_id": scenario_id, "runs": [asdict(item) for item in runs]}),
            cli_available=(artifact / "src/yuyao/__main__.py").is_file(),
            runs=tuple(runs),
            artifact_files=observed_files,
            repository_root=_WORKSPACE,
            # The identifier the dispatch reported.  A point that asks whether
            # stopping a session takes effect has to know which session it
            # stopped, and only the dispatch output carries that.
            agent_session_id=agent_session_id,
            session_start_error=str(session_facts.get("start_error") or ""),
            checkpoint_id=str(session_facts.get("checkpoint_id") or ""),
            pre_listing=str(session_facts.get("pre_listing") or ""),
            selection_command=str(session_facts.get("selection_command") or ""),
            selection_result=str(session_facts.get("selection_result") or ""),
            driver_error="" if runs else "HEADLESS_NO_RUN",
            process_tree_clean=all(not item.timed_out for item in runs),
            workspace_clean=True,
        )
    server.shutdown(); server.server_close(); thread.join(timeout=2)
    judgement = owner.judge(observation)
    return {"observation": observation, "judgement": judgement,
            "module": owner.module, "definition_digest": definition_digest(owner.definition)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = _run_one()
    Path(os.environ["SBH_TYPED_EVENT_PATH"]).write_text(json.dumps({
        "schema": "doc2harness.typed-events/1", "source_digest": os.environ["SBH_SOURCE_DIGEST"],
        "task_digest": os.environ["SBH_TASK_DIGEST"], "budget_digest": os.environ["SBH_BUDGET_DIGEST"],
        "artifact_digest": os.environ["SBH_ARTIFACT_DIGEST"], "artifact_id": os.environ["SBH_ARTIFACT_ID"],
        "scenario_id": os.environ["SBH_SCENARIO_ID"], "scenario_module": result["module"],
        "definition_digest": result["definition_digest"],
        "events": [{"type": "headless-observation", "observation": asdict(result["observation"]),
                    "judgement": asdict(result["judgement"])}],
    }, ensure_ascii=False, default=lambda value: value.value if hasattr(value, "value") else str(value), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
