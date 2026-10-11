"""Shared evidence helpers for the production drivers.

They stay deliberately narrow: digest evidence, read the artifact's own status
document, collect trajectory text, summarise recorded gateway requests, and
invoke the artifact's public entrypoint.  None of them decides an outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from doc2harness.runtime.resource_limits import container_memory_flags


# Where _run_artifact mounts the workspace inside the evaluation container.
# Exported because the task document must name this path, not the host one.
CONTAINER_WORKSPACE = "/sbh-workspace"

def evidence_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        b"doc2harness.direct-mock-production-driver/1\0"
        + json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def read_status_document(out_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((out_dir / "status.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    # Artifacts report either ``status`` or ``exit_status``; normalize the
    # explicit status document, never infer
    # a verdict from free-form stdout.
    if "exit_status" not in value and isinstance(value.get("status"), str):
        value["exit_status"] = value["status"]
    return value


def read_trajectory_text(root: Path) -> str:
    chunks: list[str] = []
    if not root.exists():
        return ""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            continue
        names = {part.lower() for part in path.parts}
        if ".trajectory" not in names and "trajectory" not in path.name.lower() and path.name not in {
            "events.jsonl", "steps.jsonl", "trace.md"
        }:
            continue
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def request_facts(records: list[dict[str, Any]], token: str | None):
    from doc2harness.benchmark.harnesses.direct.contracts import DirectMockRequestFact

    facts = []
    for record in records:
        probes = record.get("probes") or {}
        result_texts = tuple(str(item) for item in (record.get("tool_result_texts") or ()))
        # The gateway retains the full role sequence, while the public typed
        # contract intentionally caps this diagnostic at 32 entries.  Keep
        # the bounded tail rather than letting a long session invalidate the
        # entire catalog receipt.
        message_roles = tuple(str(item) for item in (record.get("msg_roles") or ()))
        facts.append(
            DirectMockRequestFact(
                body_bytes=int(record.get("body_bytes", 0)),
                has_tools=bool(record.get("has_tools")),
                tool_names=tuple(str(item) for item in (record.get("tool_names") or ())),
                probe_seen=bool(probes.get(token)) if token else False,
                message_count=int(record.get("n_messages", 0)),
                message_roles=message_roles[-32:],
                path=str(record.get("path") or ""),
                model=str(record.get("model") or ""),
                nonleading_system=bool(record.get("nonleading_system")),
                orphan_tool_result=bool(record.get("orphan_tool_result")),
                lone_surrogate=bool(record.get("lone_surrogate")),
                has_any_null=bool(record.get("has_any_null")),
                empty_content_block=bool(record.get("empty_content_block")),
                dangling_tool_use=bool(record.get("dangling_tool_use")),
                trailing_empty_assistant=bool(record.get("trailing_empty_assistant")),
                tool_result_max_bytes=int(record.get("tool_result_max_bytes", 0)),
                target_tool_result=next((item for item in reversed(result_texts) if item), ""),
                has_reasoning=bool(record.get("has_reasoning")),
            )
        )
    return tuple(facts)


def run_artifact_entrypoint(
    artifact: Path,
    workspace: Path,
    home: Path,
    output: Path,
    task: Path,
    budget: Path,
    argv: list[str],
    env: dict[str, str],
    port: int,
    timeout: int,
    stale_cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run the unchanged artifact inside the reviewed evaluation image.

    The artifact source is mounted read-only; only the declared repository,
    home and output directories are writable.  This keeps the production path
    from accidentally succeeding because the host happens to have the
    artifact's Python dependencies installed.
    """

    image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
    container_env = {
        "HOME": "/sbh-home",
        "YUYAO_HOME": "/sbh-home/.yuyao",
        "PYTHONDONTWRITEBYTECODE": "1",
        "D2A_MODEL_BASE_URL": f"http://host.docker.internal:{port}/v1",
        # The other two halves of the model configuration.  A product is
        # entitled to treat "configured" as all three being present, and one
        # does exactly that (``model.is_configured`` requires name and key as
        # well) - so with only the URL set, its sub-agent declines to start and
        # answers "[sub-agent could not run: model not configured]".  Points
        # about what a sub-agent does then pass vacuously: nothing escaped the
        # step budget because nothing ran.  So all three are set.
        "D2A_MODEL_NAME": "litellm_proxy/mock",
        "D2A_MODEL_API_KEY": "sk-mock-test",
    }
    command = [
        "docker", "run", *container_memory_flags(), "--rm", "--init",
        "--network", "bridge",
        "--add-host=host.docker.internal:host-gateway",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{artifact}:/sbh-artifact:ro",
        "-v", f"{workspace}:/sbh-workspace:rw",
        "-v", f"{home}:/sbh-home:rw",
        "-v", f"{output}:/sbh-output:rw",
    ]
    if stale_cwd is not None:
        command.extend(("-v", f"{stale_cwd}:/sbh-stale:rw", "-w", "/sbh-stale"))
    forwarded = {
        k: v for k, v in env.items()
        if k.startswith("D2A_") and k not in {"D2A_MODEL_BASE_URL", "D2A_MODEL_NAME", "D2A_MODEL_API_KEY"}
    }
    # A point that observes how an external tool was launched puts a recording
    # wrapper ahead of the real binary on PATH.  The wrapper execs the real one,
    # so nothing about the search changes - but without the PATH the launch is
    # unobservable and the point cannot tell a mishandled invocation from an
    # absent observation.
    if env.get("SBH_CONTAINER_PATH"):
        forwarded["PATH"] = str(env["SBH_CONTAINER_PATH"])
    # A hermeticity point needs the ambient configuration it is about to
    # actually be present: with nothing to be sealed from, a product that reads
    # whatever config it finds is indistinguishable from one that seals.
    if env.get("SBH_CONTAINER_RG_CONFIG"):
        forwarded["RIPGREP_CONFIG_PATH"] = str(env["SBH_CONTAINER_RG_CONFIG"])
        forwarded["RG_CONFIG_PATH"] = str(env["SBH_CONTAINER_RG_CONFIG"])
    if env.get("SBH_CONTAINER_RG_HOME"):
        forwarded["HOME"] = str(env["SBH_CONTAINER_RG_HOME"])
        forwarded["XDG_CONFIG_HOME"] = str(env["SBH_CONTAINER_RG_HOME"]) + "/.config"
    # The proxy point works by putting a trap proxy in the ambient environment
    # and requiring the explicit model endpoint to reach the gateway anyway,
    # so the proxy variables have to reach the container: forwarding only
    # ``D2A_*`` names would leave the product no proxy to honour or bypass.
    # Rewrite the loopback address the host chose into the one the container
    # can reach.
    #
    # Only the trap proxy this driver started is rewritten.  A scenario may
    # instead declare a proxy of its own - the offline point names a dead port
    # and exempts loopback through ``NO_PROXY`` - and there the loopback
    # address is the literal subject of the test.  Rewriting it moves the
    # gateway out from behind that exemption, so every request is sent to a
    # closed port and a healthy product fails to start at all.  Those values
    # are forwarded exactly as declared.
    trap = str(env.get("SBH_TRAP_PROXY_URL") or "")
    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
        "NO_PROXY", "no_proxy",
    ):
        if name in env:
            value = str(env[name])
            # Rewrite only the trap this driver started; a scenario's own
            # declared proxy travels verbatim.
            if trap and value == trap:
                value = value.replace(
                    "http://127.0.0.1:", "http://host.docker.internal:", 1
                )
            forwarded[name] = value
    # A scenario that exempts loopback from its own proxy is naming the
    # gateway: when the harness and the product share a host, the gateway
    # *is* 127.0.0.1 and the exemption covers it.  In a container the gateway
    # answers on ``host.docker.internal``, which that exemption does not name,
    # so every model request is handed to the scenario's deliberately dead
    # proxy and a healthy product cannot start.  Extend the exemption to the
    # address the gateway actually has here, preserving what it meant.
    for name in ("NO_PROXY", "no_proxy"):
        value = forwarded.get(name)
        if value and "127.0.0.1" in value and "host.docker.internal" not in value:
            forwarded[name] = value + ",host.docker.internal"
    for key, value in {**container_env, **forwarded}.items():
        command.extend(("-e", f"{key}={value}"))
    command.extend((image, "/sbh-artifact/run_agent.sh"))
    translated = {
        str(task): f"{CONTAINER_WORKSPACE}/task.json",
        str(workspace): CONTAINER_WORKSPACE,
        str(budget): "/sbh-workspace/budget.json",
        str(output): "/sbh-output",
    }
    for item in argv[1:]:
        if item.startswith("http://127.0.0.1:"):
            command.append(item.replace("http://127.0.0.1:", "http://host.docker.internal:", 1))
        else:
            command.append(translated.get(item, item))
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
        return completed, False
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(command, -1, exc.stdout or "", exc.stderr or ""), True


__all__ = [
    "evidence_digest",
    "read_status_document",
    "read_trajectory_text",
    "request_facts",
    "run_artifact_entrypoint",
]
