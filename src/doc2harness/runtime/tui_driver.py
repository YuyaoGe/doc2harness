"""Run one active ``tui`` scenario in the evaluation container."""

from __future__ import annotations

import argparse
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

from doc2harness.runtime import mock_gateway
from doc2harness.runtime.tui_stimulus import tui_gateway_config
from typing import Any

from doc2harness.runtime.direct_mock_driver import _digest, _trajectory
from doc2harness.runtime.resource_limits import container_memory_flags


def _read(master: int, buffer: bytearray, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            buffer.extend(os.read(master, 65536))
        except OSError:
            return


def _read_until_quiet(
    master: int, buffer: bytearray, max_wait: float, quiet_for: float = 0.35
) -> None:
    """Read until the terminal stops producing output, or the budget runs out.

    A fixed pause between typed lines is not synchronisation.  A line typed
    while the previous turn is still answering is taken as literal text in the
    input box rather than submitted, so the turn the point is about never
    reaches the model and the point reports its own stimulus as unobserved.
    """

    deadline = time.monotonic() + max_wait
    stable_since = time.monotonic()
    last = len(buffer)
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            try:
                buffer.extend(os.read(master, 65536))
            except OSError:
                return
        if len(buffer) != last:
            last = len(buffer)
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= quiet_for:
            return


def _file_observations(workspace: Path, paths: tuple[str, ...]):
    from doc2harness.benchmark.harnesses.tui.contracts import ObservedFile

    result = []
    for relative in paths:
        path = workspace / relative
        if path.is_file():
            result.append(ObservedFile(relative, True, path.read_text(encoding="utf-8", errors="replace")))
        else:
            result.append(ObservedFile(relative, False, ""))
    return tuple(result)


def _run_one() -> dict[str, Any]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.tui.adapter import build_tui_pty_execution_plan
    from doc2harness.benchmark.harnesses.tui.contracts import (
        PromptObservation, TuiObservation, TuiRequest,
    )

    scenario_id = os.environ["SBH_SCENARIO_ID"]
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.tui." not in owner.module:
        raise RuntimeError("TUI driver received a non-TUI scenario")
    scenario = owner.scenario
    plan = build_tui_pty_execution_plan(scenario)
    recipe = scenario.recipe
    artifact = Path(os.environ["SBH_ARTIFACT_PATH"])
    gateway_module = mock_gateway
    cfg = tui_gateway_config(scenario, gateway_module)
    server = gateway_module.ThreadingHTTPServer(("0.0.0.0", 0), gateway_module.Handler)
    server.cfg = cfg
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic_ns()
    screen = bytearray()
    timed_out = False
    driver_error = ""
    returncode: int | None = None
    with tempfile.TemporaryDirectory(prefix="sbh-tui-production-") as temporary:
        root = Path(temporary)
        home, workspace = root / "home", root / "workspace"
        home.mkdir(); workspace.mkdir()
        for seed in recipe.seed_files:
            path = workspace / seed.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes.fromhex(seed.data_hex))
        env = dict(os.environ)
        env.update({
            "HOME": str(home), "YUYAO_HOME": str(home / ".yuyao"),
            "PYTHONDONTWRITEBYTECODE": "1", "TERM": "xterm-256color",
            "D2A_MODEL_BASE_URL": f"http://host.docker.internal:{server.server_address[1]}/v1",
            "D2A_MODEL_NAME": "mock/release-tui", "D2A_MODEL_API_KEY": "sk-mock-test",
        })
        image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
        command = [
            "docker", "run", *container_memory_flags(), "--rm", "--init", "-it", "--network", "bridge",
            "--add-host=host.docker.internal:host-gateway", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{artifact}:/sbh-artifact:ro", "-v", f"{workspace}:/sbh-workspace:rw",
            # Start in the writable workspace, as every other driver does.
            # Without this the container's working directory is the read-only
            # root, so a shell line that redirects into a relative path fails
            # with exit 1 and a point about "the shell really ran" can only
            # observe an absent file - it was judging the mount, not the
            # product.
            "-w", "/sbh-workspace",
            "-v", f"{home}:/sbh-home:rw", "-e", "HOME=/sbh-home",
            "-e", "YUYAO_HOME=/sbh-home/.yuyao", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "TERM=xterm-256color",
            "-e", f"D2A_MODEL_BASE_URL=http://host.docker.internal:{server.server_address[1]}/v1",
            "-e", "D2A_MODEL_NAME=mock/release-tui", "-e", "D2A_MODEL_API_KEY=sk-mock-test",
            "-e", "PYTHONPATH=/sbh-artifact/src", image,
            "python3", "/sbh-artifact/src/yuyao/__main__.py", "--yolo", "--stream", "off",
            *recipe.cli_arguments,
        ]
        master, slave = pty.openpty()
        process = subprocess.Popen(command, cwd=workspace, env=env, stdin=slave, stdout=slave,
                                   stderr=slave, start_new_session=True)
        os.close(slave)
        try:
            _read(master, screen, 3.0)
            for item in recipe.input_lines:
                payload = bytes.fromhex(item.data_hex)
                os.write(master, payload)
                if not payload.endswith((b"\r", b"\n")):
                    os.write(master, b"\r")
                # Wait for the turn to finish answering before typing the next
                # line, rather than pausing a fixed second: a line typed into a
                # busy prompt is kept as text instead of submitted.
                _read_until_quiet(master, screen, min(float(recipe.timeout_seconds), 30.0))
            # The survival sentinel: after the declared input the driver feeds
            # one more line that echoes a completion token. Seeing it
            # is what proves the session still processes input after whatever the
            # point threw at it - a crash, a hang, or a mode it cannot leave all
            # keep it from appearing.
            completion = getattr(recipe.oracle, "completion_token", "")
            if completion:
                os.write(master, ("!printf '" + completion + chr(92) + chr(92) + "n'").encode())
                os.write(master, b"\r")
                _read(master, screen, 2.0)
            _read(master, screen, min(float(recipe.timeout_seconds), 20.0))
            os.write(master, b"\x04")
            _read(master, screen, 3.0)
            # Give the exit whatever the recipe still allows rather than a flat
            # three seconds.  A product that is mid-loop when EOF arrives has to
            # finish its current round before it can leave, and on a loaded host
            # - a full catalog run puts sixteen of these containers on the CPU at
            # once - that takes longer in wall-clock than it does when the
            # scenario runs alone.  A flat cap would make the verdict depend on
            # how busy the machine is.
            spent = (time.monotonic_ns() - started) / 1e9
            remaining = max(3.0, float(recipe.timeout_seconds) - spent)
            returncode = process.wait(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired) as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            driver_error = type(exc).__name__
            try:
                os.killpg(process.pid, signal.SIGTERM)
                returncode = process.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                returncode = -1
        finally:
            os.close(master)
        trajectory = _trajectory(home) + "\n" + _trajectory(workspace)
        screen_text = screen.decode("utf-8", errors="replace")
        requests = tuple(TuiRequest(json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in cfg.get("requests", []))
        prompt = PromptObservation(
            identity=f"pty-{process.pid}", turn_id="turn-1", candidate_boundary=True,
            canary_nonce_absent_before=True, canary_nonce_absent_from_input=True,
            canary_observed=any(token in screen_text or token in trajectory for token in recipe.probe_tokens),
            input_consumed=bool(requests), startup_returncode=returncode,
            timeline=("pty_open", "request_observed" if requests else "no_request", "pty_close"),
        )
        observation = TuiObservation(
            scenario_id=scenario_id, profile=recipe.profile, plan_digest=plan.plan_digest,
            artifact_id=os.environ["SBH_ARTIFACT_ID"], artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
            backend_evidence_digest=_digest({"scenario_id": scenario_id, "requests": len(requests), "returncode": returncode, "screen_sha256": hashlib.sha256(screen).hexdigest()}),
            cli_available=(artifact / "src/yuyao/__main__.py").is_file(), prompt=prompt,
            screen_text=screen_text, screen_sha256=hashlib.sha256(screen).hexdigest(), screen_size=len(screen),
            returncode=returncode, signal=None, requests=requests,
            files=_file_observations(workspace, recipe.observed_files), elapsed_ns=time.monotonic_ns() - started,
            deadline_reached=timed_out, driver_error=driver_error,
            process_tree_clean=not timed_out and not driver_error, workspace_clean=True,
            trajectory_text=trajectory,
        )
    server.shutdown(); server.server_close(); thread.join(timeout=2)
    return {"observation": observation, "judgement": owner.judge(observation),
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
        "events": [{"type": "tui-pty-observation", "observation": asdict(result["observation"]),
                    "judgement": asdict(result["judgement"])}],
    }, ensure_ascii=False, default=lambda value: value.value if hasattr(value, "value") else str(value), indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
