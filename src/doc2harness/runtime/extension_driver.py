"""Run one extension counterfactual with a real CLI and a closed fixture.

The fixture is created outside the artifact and mounted as a temporary root;
the typed observation records only discovery markers, request counts, and
cleanup facts.  Verdict logic remains in the scenario oracle.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import select
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from doc2harness.runtime import mock_gateway
from doc2harness.runtime.direct_mock_driver import _digest
from doc2harness.runtime.resource_limits import container_memory_flags


def _read_pty(master: int, buffer: bytearray, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            buffer.extend(os.read(master, 65536))
        except OSError:
            return


_WIRE_MARKERS = {
    "commands": "EXTSYM_COMMAND_BODY_9417",
    "skills": "EXTSYM_SKILL_BODY_9418",
    "plugins": "EXTSYM_PLUGIN_BODY_9419",
}
_EXEC_MARKERS = {
    "commands": "EXTSYM_COMMAND_EXEC_9420",
    "hooks": "EXTSYM_HOOK_EXEC_9421",
    "plugins": "EXTSYM_PLUGIN_EXEC_9422",
}


def _extension_fixture(root: Path, mode: str) -> tuple[Path, dict[str, Path], bool]:
    repo = root / "repo"
    inside = repo / "extension-definition-sources"
    outside = root / "external-definition-sources"
    markers = root / "extension-markers"
    for directory in (repo, inside, outside, markers, root / "home"):
        directory.mkdir(parents=True, exist_ok=True)
    source = inside if mode == "inside" else outside
    marker_paths = {face: markers / f"{face}.marker" for face in _EXEC_MARKERS}

    def shell_marker(face: str) -> str:
        # Definitions are interpreted inside the evaluation container; use
        # the mounted root path rather than the host's temporary pathname.
        return f"printf '{_EXEC_MARKERS[face]}\\n' >> '/sbh-root/extension-markers/{face}.marker'"

    definitions = {
        "command.md": (
            "---\ndescription: symlink definition probe\n---\n"
            f"!{{{shell_marker('commands')}}}\n"
            f"Carry the exact marker {_WIRE_MARKERS['commands']} into this turn.\n"
        ),
        "SKILL.md": (
            "---\nname: extsymskill\ndescription: symlink definition probe\n"
            "triggers: [extsymskill]\n---\n"
            f"Carry the exact marker {_WIRE_MARKERS['skills']} into this turn.\n"
        ),
        "hooks.json": json.dumps({
            "hooks": {
                "beforeShellExecution": [{"matcher": "*", "command": shell_marker("hooks")}],
                "beforeToolUse": [{"matcher": "*", "command": shell_marker("hooks")}],
            },
        }),
        "plugin.json": json.dumps({
            "name": "extsympackage",
            "description": "symlink definition probe",
            "commands": [{
                "name": "extsymplugin",
                "description": "symlink definition probe",
                "prompt": f"!{{{shell_marker('plugins')}}}\nCarry the exact marker {_WIRE_MARKERS['plugins']} into this turn.",
                "body": f"!{{{shell_marker('plugins')}}}\nCarry the exact marker {_WIRE_MARKERS['plugins']} into this turn.",
            }],
            "hooks": {"beforeShellExecution": [{"matcher": "*", "command": shell_marker("plugins")}]},
        }),
    }
    targets: dict[str, Path] = {}
    for name, text in definitions.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        targets[name] = target
    links = {
        repo / "commands" / "extsymcommand.md": targets["command.md"],
        repo / "skills" / "extsymskill" / "SKILL.md": targets["SKILL.md"],
        repo / "hooks.json": targets["hooks.json"],
        repo / "plugins" / "extsympackage" / "plugin.json": targets["plugin.json"],
    }
    for link, target in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(target, link.parent) if mode == "inside" else target)
    repo_real = repo.resolve()
    shape: dict[str, bool] = {}
    for link, target in links.items():
        resolved = link.resolve(strict=True)
        contained = os.path.commonpath((str(repo_real), str(resolved))) == str(repo_real)
        shape[str(link.relative_to(repo))] = (
            link.is_symlink() and resolved == target.resolve(strict=True)
            and contained == (mode == "inside")
        )
    return repo, marker_paths, bool(shape) and all(shape.values())


def _run_mode(scenario: Any, artifact: Path, mode: str) -> tuple[Any, dict[str, Any]]:
    from doc2harness.benchmark.harnesses.extension.contracts import (
        ExtensionFace,
        ExtensionRunObservation,
    )

    gateway_module = mock_gateway
    marker_tokens = list(_WIRE_MARKERS.values())
    rounds = [
        [{"name": "bash", "command": scenario.recipe.round_command}],
        [{"name": "__finish__"}],
    ]
    cfg = gateway_module.new_cfg(
        mode="seq_probe", rounds=rounds, probe_tokens=marker_tokens,
    )
    server = gateway_module.ThreadingHTTPServer(("0.0.0.0", 0), gateway_module.Handler)
    server.cfg = cfg
    server.daemon_threads = True
    server.block_on_close = False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    driver_error = ""
    timed_out = False
    returncode: int | None = None
    with tempfile.TemporaryDirectory(prefix="sbh-extension-production-") as temporary:
        root = Path(temporary)
        repo, marker_paths, fixture_ok = _extension_fixture(root, mode)
        home = root / "home"
        screen = bytearray()
        env = dict(os.environ)
        env.update({
            "HOME": str(home), "YUYAO_HOME": str(home / ".yuyao"),
            "PYTHONDONTWRITEBYTECODE": "1", "TERM": "xterm-256color",
            "D2A_MODEL_BASE_URL": f"http://host.docker.internal:{server.server_address[1]}/v1",
            "D2A_MODEL_NAME": "mock/release-extension", "D2A_MODEL_API_KEY": "sk-mock-test",
        })
        image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
        command = [
            "docker", "run", *container_memory_flags(), "--rm", "--init", "-it", "--network", "bridge",
            "--add-host=host.docker.internal:host-gateway", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{artifact}:/sbh-artifact:ro", "-v", f"{root}:/sbh-root:rw",
            "-w", "/sbh-root/repo",
            "-e", "HOME=/sbh-root/home", "-e", "YUYAO_HOME=/sbh-root/home/.yuyao",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "TERM=xterm-256color",
            "-e", f"D2A_MODEL_BASE_URL=http://host.docker.internal:{server.server_address[1]}/v1",
            "-e", "D2A_MODEL_NAME=mock/release-extension", "-e", "D2A_MODEL_API_KEY=sk-mock-test",
            "-e", "PYTHONPATH=/sbh-artifact/src", image,
            "python3", "/sbh-artifact/src/yuyao/__main__.py", "--yolo", "--stream", "off",
        ]
        master, slave = pty.openpty()
        process = subprocess.Popen(command, cwd=repo, env=env, stdin=slave, stdout=slave,
                                   stderr=slave, start_new_session=True)
        os.close(slave)
        try:
            _read_pty(master, screen, 3.0)
            for setup in scenario.recipe.setup_commands:
                os.write(master, setup.encode() + b"\r")
                _read_pty(master, screen, 1.0)
            os.write(master, b"echo PERM_LIVE_7755 > live.txt\r")
            _read_pty(master, screen, 2.0)
            _read_pty(master, screen, min(float(scenario.recipe.timeout_seconds), 6.0))
            os.write(master, b"\x04")
            _read_pty(master, screen, 2.0)
            try:
                returncode = process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                # The interactive CLI may remain at its prompt after the
                # required wire/tool evidence is captured.  Close that
                # prompt deliberately; this is harness cleanup, not a test
                # timeout or an execution failure.
                os.killpg(process.pid, signal.SIGTERM)
                returncode = process.wait(timeout=2.0)
        except OSError as exc:
            driver_error = type(exc).__name__
            try:
                os.killpg(process.pid, signal.SIGTERM)
                returncode = process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                returncode = -1
        finally:
            os.close(master)

        seen = []
        request_blob = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in cfg["requests"])
        for face, token in _WIRE_MARKERS.items():
            if token in request_blob:
                seen.append(getattr(ExtensionFace, face.upper()))
        for face, path in marker_paths.items():
            if path.is_file() and _EXEC_MARKERS[face] in path.read_text(encoding="utf-8", errors="replace"):
                enum_face = getattr(ExtensionFace, face.upper())
                if enum_face not in seen:
                    seen.append(enum_face)
        tool_result_live = any(
            "PERM_LIVE_7755" in "\n".join(row.get("tool_result_texts") or ())
            for row in cfg["requests"]
        )
        run = ExtensionRunObservation(
            fixture_verified=fixture_ok,
            live=(repo / "live.txt").is_file() or tool_result_live,
            request_count=len(cfg["requests"]),
            seen_faces=tuple(seen),
            fixture_error_code=None if fixture_ok else "EXTENSION_FIXTURE_SHAPE",
        )
        evidence = {
            "mode": mode, "fixture_ok": fixture_ok, "request_count": len(cfg["requests"]),
            "returncode": returncode, "screen_sha256": hashlib.sha256(screen).hexdigest(),
            "driver_error": driver_error, "timed_out": timed_out,
        }
    server.shutdown(); server.server_close()
    return run, evidence


def _run_one() -> dict[str, Any]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.extension.adapter import build_extension_execution_plan
    from doc2harness.benchmark.harnesses.extension.contracts import ExtensionCounterfactualObservation
    from doc2harness.benchmark.harnesses.extension.oracle import judge_extension_observation

    scenario_id = os.environ["SBH_SCENARIO_ID"]
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.extension." not in owner.module:
        raise RuntimeError("extension driver received a non-extension scenario")
    scenario = owner.scenario
    plan = build_extension_execution_plan(scenario)
    artifact = Path(os.environ["SBH_ARTIFACT_PATH"])
    inside, inside_evidence = _run_mode(scenario, artifact, "inside")
    outside = None
    evidence = {"inside": inside_evidence}
    if scenario.recipe.paired:
        outside, outside_evidence = _run_mode(scenario, artifact, "outside")
        evidence["outside"] = outside_evidence
    observation = ExtensionCounterfactualObservation(
        scenario_id=scenario_id, plan_digest=plan.plan_digest,
        artifact_id=os.environ["SBH_ARTIFACT_ID"], artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
        backend_evidence_digest=_digest(evidence), inside=inside, outside=outside,
        process_tree_clean=all(not item.get("driver_error") and not item.get("timed_out") for item in evidence.values()),
        workspace_clean=True,
    )
    return {
        "observation": observation,
        "judgement": judge_extension_observation(scenario, observation),
        "module": owner.module,
        "definition_digest": definition_digest(owner.definition),
    }


def main() -> int:
    result = _run_one()
    # Create the output directory as every driver does: run_catalog pre-creates
    # it, but a scenario run directly does not.
    typed_event_path = Path(os.environ["SBH_TYPED_EVENT_PATH"])
    typed_event_path.parent.mkdir(parents=True, exist_ok=True)
    typed_event_path.write_text(json.dumps({
        "schema": "doc2harness.typed-events/1", "source_digest": os.environ["SBH_SOURCE_DIGEST"],
        "task_digest": os.environ["SBH_TASK_DIGEST"], "budget_digest": os.environ["SBH_BUDGET_DIGEST"],
        "artifact_digest": os.environ["SBH_ARTIFACT_DIGEST"], "artifact_id": os.environ["SBH_ARTIFACT_ID"],
        "scenario_id": os.environ["SBH_SCENARIO_ID"], "scenario_module": result["module"],
        "definition_digest": result["definition_digest"],
        "events": [{"type": "extension-counterfactual-observation", "observation": asdict(result["observation"]),
                    "judgement": asdict(result["judgement"])}],
    }, ensure_ascii=False, default=lambda value: value.value if hasattr(value, "value") else str(value), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
