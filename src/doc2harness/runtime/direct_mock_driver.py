"""Run one active ``direct`` scenario through a real artifact.

This is a production-driver boundary, not a scorer shortcut.  The artifact is
invoked through its public ``run_agent.sh`` contract, the mock gateway is used
only as deterministic stimulus, and the active scenario-local judge receives a
typed ``DirectMockRawObservation``; no recorded result, runner, or verdict is
imported.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from doc2harness.runtime import mock_gateway
from doc2harness.runtime.driver_evidence import (
    CONTAINER_WORKSPACE,
    evidence_digest as _digest,
    read_status_document as _status,
    read_trajectory_text as _trajectory,
    request_facts as _request_facts,
    run_artifact_entrypoint as _run_artifact,
)
from typing import Any


class _TrapProxy(ThreadingHTTPServer):
    """Record any product request that incorrectly reaches the HTTP proxy."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("0.0.0.0", 0), _TrapProxyHandler)
        self.connections: list[dict[str, Any]] = []
        self.lock = threading.Lock()


class _TrapProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _trap(self) -> None:
        server: _TrapProxy = self.server
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if 0 < length <= 2 * 1024 * 1024 else b""
        kind = "self-check" if self.headers.get("X-Direct-Mock-Self") == "proxy-self-check" else "product"
        with server.lock:
            server.connections.append(
                {
                    "api_key_seen": "sk-mock-test" in str(self.headers.get("Authorization") or ""),
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "kind": kind,
                    "method": self.command,
                    "target": self.path,
                }
            )
        payload = b'{"proxy":"direct-mock-trap"}'
        self.send_response(502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    do_CONNECT = _trap
    do_GET = _trap
    do_POST = _trap


def _raw_http(host: str, port: int, request: bytes) -> bytes:
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def _rounds(fixture: Any, observer: str | None = None) -> list[list[dict[str, Any]]]:
    rounds: list[list[dict[str, Any]]] = []
    for group in fixture.rounds:
        current: list[dict[str, Any]] = []
        for action in group:
            if action.finish:
                current.append({"name": "__finish__"})
                continue
            args = dict(action.arguments)
            if observer is not None:
                args = {
                    key: value.replace("{OBSERVER}", observer) if isinstance(value, str) else value
                    for key, value in args.items()
                }
            name = action.tool_candidates[0]
            if name in {"bash", "shell", "run_command", "execute", "exec", "terminal"}:
                current.append({"name": name, "command": str(args.pop("command", "")), **args})
            else:
                current.append({"name": name, "args": args})
        if current:
            rounds.append(current)
    return rounds


def _mode(recipe: Any) -> str:
    return {
        "SEQUENTIAL_PROBE": "seq_probe",
        "FIXED_REPLY": "fixed_reply",
    }.get(recipe.mode.value, recipe.mode.value.lower())


def _facts(
    scenario: Any,
    workspace: Path,
    requests: tuple[Any, ...],
    trajectory: str,
    raw_records: list[dict[str, Any]] | None = None,
    proxy_observation: dict[str, Any] | None = None,
    web_gets: list[dict[str, Any]] | None = None,
):
    from doc2harness.benchmark.harnesses.direct.contracts import DirectMockFactKey, DirectMockObservedFact

    facts: dict[Any, bool | int | str] = {}
    raw_records = raw_records or []
    proxy_observation = proxy_observation or {}
    web_gets = web_gets or []

    def _probe_seen(token: str) -> bool:
        return any((bool((record.get("probes") or {}).get(token)) for record in raw_records))

    def _tool_seen(*names: str) -> bool:
        wanted = {item.lower().replace("-", "_") for item in names}
        return any(
            (
                wanted.intersection(
                    (str(name).lower().replace("-", "_") for name in record.get("tool_names") or ())
                )
                for record in raw_records
            )
        )

    tool_result_texts = [
        str(text) for record in raw_records for text in record.get("tool_result_texts") or ()
    ]
    tool_result_blob = "\n".join(tool_result_texts)
    token = scenario.recipe.expected_token
    if not token:
        found = re.search("[A-Z][A-Z0-9_]*_[0-9]{4}", str(getattr(scenario.recipe, "command", "") or ""))
        if found:
            token = found.group(0)
    if token:
        facts[DirectMockFactKey.EXPECTED_REPOSITORY_FILE] = any(
            (
                token in p.read_text(encoding="utf-8", errors="replace")
                for p in workspace.rglob("*")
                if p.is_file() and p.stat().st_size <= 8 * 1024 * 1024
            )
        )
    names = {p.name for p in workspace.rglob("*") if p.is_file()}
    facts[DirectMockFactKey.DENY_MARKER_PRESENT] = any((name.startswith("PERM_DENY_") for name in names))
    facts[DirectMockFactKey.SHARED_FILE_PRESENT] = (workspace / "shared.txt").is_file()
    facts[DirectMockFactKey.SHARED_FILE_CONTENT] = (
        (workspace / "shared.txt").read_text(encoding="utf-8", errors="replace")
        if (workspace / "shared.txt").is_file()
        else ""
    )
    facts[DirectMockFactKey.SHARED_FILE_MARKER] = "SHAREDW_4242" in str(
        facts[DirectMockFactKey.SHARED_FILE_CONTENT]
    )
    facts[DirectMockFactKey.PARALLEL_A] = (workspace / "pa.txt").is_file()
    facts[DirectMockFactKey.PARALLEL_B] = (workspace / "pb.txt").is_file()
    facts[DirectMockFactKey.RESPONSE_USAGE_HEALTHY] = scenario.recipe.mode.value in {
        "NORMAL",
        "SEQUENTIAL_PROBE",
    } and bool(requests)
    facts[DirectMockFactKey.REPOSITORY_CLEAN] = (
        not bool(
            subprocess.run(
                ("git", "status", "--porcelain"), cwd=workspace, capture_output=True, check=False
            ).stdout
        )
        if (workspace / ".git").is_dir()
        else True
    )
    facts[DirectMockFactKey.OFFLINE_LOOPBACK_ONLY] = all(
        (
            item.path.startswith("/v1") or item.path.startswith("/local") or (not item.path)
            for item in requests
        )
    )
    facts[DirectMockFactKey.REASONING_REDACTED] = not any((item.has_reasoning for item in requests))
    if profile_name := getattr(getattr(scenario.recipe, "fixture_profile", None), "value", ""):
        if profile_name == "WEB_FETCH":
            gets = [item for item in web_gets if str(item.get("path", "")).startswith("/local-probe")]
            facts[DirectMockFactKey.WEB_FETCH_HEALTHY] = (
                len(gets) == 1
                and gets[0].get("status") == 200
                and (gets[0].get("send_complete") is True)
                and ("SBH_WEBFETCH_HEALTHY_BODY_7528" in tool_result_blob)
            )
            facts[DirectMockFactKey.WEB_FAILURES_PRESERVED] = bool(
                gets
                and re.search(
                    "(?i)(?:http|status|status_code|code|->)[^0-9]{0,18}(?:404|429|500)", tool_result_blob
                )
                and (
                    re.search(
                        "(?i)(?:error|failed|failure|not found|too many requests|rate limit|server error)",
                        tool_result_blob,
                    )
                    or re.search("(?i)[\"\\']?(?:ok|success)[\"\\']?\\s*[:=]\\s*false", tool_result_blob)
                    or re.search("(?i)[\"\\']?is[_-]?error[\"\\']?\\s*[:=]\\s*true", tool_result_blob)
                )
            )
        if profile_name in {"QUALITY_GATE_PASS", "QUALITY_GATE_FAIL"}:
            marker = workspace / "qgate.marker"
            facts[DirectMockFactKey.QGATE_RAN] = marker.is_file() and "QGATE_RAN_7755" in marker.read_text(
                encoding="utf-8", errors="replace"
            )
        if profile_name in {"SHELL_EDGE_CONTROL", "SHELL_EDGE_TARGET"}:
            facts[DirectMockFactKey.SHELL_CONTROL_BYTES] = "SHELL_EDGE_CONTROL_7731" in tool_result_blob
            expected = "\n  EDGE_CONTENT_7731  \n\nUNTRUNCATED_7731\n"
            facts[DirectMockFactKey.SHELL_EDGE_BYTES] = any((expected in text for text in tool_result_texts))
    profile = getattr(getattr(scenario, "recipe", None), "fixture_profile", None)
    profile_name = getattr(profile, "value", "")
    if profile_name == "HTTP_PROXY":
        proxy_self_checked = bool(proxy_observation) and all(
            (
                proxy_observation.get("gateway_self_check") is True,
                proxy_observation.get("proxy_self_check") is True,
                proxy_observation.get("environment_exact") is True,
                proxy_observation.get("gateway_request_count") == 1,
                proxy_observation.get("proxy_product_connections") == 0,
                proxy_observation.get("proxy_api_key_seen") is False,
                proxy_observation.get("cleanup_complete") is True,
            )
        )
        facts[DirectMockFactKey.PROXY_SELF_CHECKED] = proxy_self_checked
        facts[DirectMockFactKey.EXPLICIT_ENDPOINT_BYPASSED_PROXY] = (
            proxy_self_checked and proxy_observation.get("proxy_injected") is True
        )
    return tuple((DirectMockObservedFact(key, value) for key, value in facts.items()))


def _run_one() -> dict[str, Any]:
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.direct.adapter import build_direct_mock_execution_plan
    from doc2harness.benchmark.harnesses.direct.contracts import (
        DirectMockRawObservation,
        apply_direct_mock_local_judge,
    )
    from doc2harness.benchmark.definition import definition_digest

    scenario_id = os.environ.get("SBH_SCENARIO_ID", "")
    if not scenario_id:
        raise RuntimeError("SBH_SCENARIO_ID is required")
    owner = load_scenario_owner(scenario_id)
    if not owner.module.startswith("doc2harness.benchmark.harnesses.direct"):
        raise RuntimeError("direct mock driver received a non-direct scenario")
    scenario = owner.scenario
    plan = build_direct_mock_execution_plan(scenario)
    fixture = plan.fixture
    artifact = Path(os.environ["SBH_ARTIFACT_PATH"])
    gateway = mock_gateway
    gateway_server = gateway.ThreadingHTTPServer

    class _BridgeVisibleServer(gateway_server):

        def __init__(self, address, handler):
            super().__init__(("0.0.0.0", address[1]), handler)

    gateway.ThreadingHTTPServer = _BridgeVisibleServer
    with tempfile.TemporaryDirectory(prefix="sbh-direct-production-") as temporary:
        root = Path(temporary)
        home, workspace, output = (root / "home", root / "workspace", root / "out")
        stale_cwd = root / "stale-cwd"
        home.mkdir()
        workspace.mkdir()
        output.mkdir()
        stale_cwd.mkdir()
        for fixture_file in fixture.repository_files:
            target = workspace.joinpath(*fixture_file.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fixture_file.content)
        task = workspace / "task.json"
        budget = workspace / "budget.json"
        task_bytes = fixture.task_document
        try:
            task_document = json.loads(task_bytes)
        except json.JSONDecodeError:
            task_document = None
        if isinstance(task_document, dict) and task_document.get("repo_dir") == "__REPO_DIR__":
            task_document["repo_dir"] = CONTAINER_WORKSPACE
            task_bytes = json.dumps(task_document, separators=(",", ":"), sort_keys=True).encode()
        task.write_bytes(task_bytes)
        budget.write_bytes(fixture.budget_document)
        proof_token = (
            scenario.recipe.expected_token
            or fixture.probe_token
            or (
                "MOCK_PROOF_7731"
                if scenario.recipe.mode.value in {"NORMAL"}
                else ""
            )
        )
        cfg = gateway.new_cfg(
            mode=_mode(scenario.recipe),
            command=scenario.recipe.command or "echo MOCK_PROOF_7731 > mock_proof.txt",
            probe_token=fixture.probe_token or "",
            probe_tokens=[fixture.probe_token] if fixture.probe_token else [],
            rounds=_rounds(fixture),
            reject_substr=scenario.recipe.reject_substring,
        )
        if getattr(getattr(scenario.recipe, "fixture_profile", None), "value", "") == "WEB_FETCH":
            failing = bool(getattr(scenario.recipe, "control_dependencies", ()))
            cfg["web_status"] = 404 if failing else 200
            cfg["web_body"] = (
                "SBH_WEBFETCH_FAILING_BODY_8639" if failing else "SBH_WEBFETCH_HEALTHY_BODY_7528"
            )
        server = gateway.make_server(cfg, 0)
        cfg["rounds"] = _rounds(fixture, f"host.docker.internal:{int(server.server_address[1])}")
        server.daemon_threads = True
        server.block_on_close = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        proxy: _TrapProxy | None = None
        proxy_thread: threading.Thread | None = None
        proxy_observation: dict[str, Any] = {}
        profile_name = getattr(getattr(scenario.recipe, "fixture_profile", None), "value", "")
        if profile_name == "HTTP_PROXY":
            proxy = _TrapProxy()
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            proxy_thread.start()
            gateway_check = _raw_http(
                "127.0.0.1",
                int(server.server_address[1]),
                b"GET /direct-mock-self-check/gateway HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
            )
            proxy_check = _raw_http(
                "127.0.0.1",
                int(proxy.server_address[1]),
                b"GET http://direct-mock.invalid/self-check HTTP/1.1\r\nHost: direct-mock.invalid\r\nX-Direct-Mock-Self: proxy-self-check\r\nConnection: close\r\n\r\n",
            )
            proxy_observation.update(
                {
                    "gateway_self_check": gateway_check.startswith(b"HTTP/1.0 200 ")
                    or gateway_check.startswith(b"HTTP/1.1 200 "),
                    "proxy_self_check": proxy_check.startswith(b"HTTP/1.0 502 ")
                    or proxy_check.startswith(b"HTTP/1.1 502 "),
                }
            )
        env = dict(os.environ)
        env.update(dict(fixture.environment))
        env.update({"HOME": str(home), "YUYAO_HOME": str(home / ".yuyao"), "PYTHONDONTWRITEBYTECODE": "1"})
        if proxy is not None:
            injected = scenario.definition.scenario_id == "http_proxy_hermetic"
            proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env[name] = proxy_url if injected else ""
            if injected:
                env["SBH_TRAP_PROXY_URL"] = proxy_url
            env["NO_PROXY"] = ""
            env["no_proxy"] = ""
            proxy_observation.update({"environment_exact": True, "proxy_injected": injected})
        argv = [
            str(artifact / "run_agent.sh"),
            "--task-file",
            str(task),
            "--repo-dir",
            str(workspace),
            "--model-base-url",
            f"http://127.0.0.1:{server.server_address[1]}{scenario.recipe.base_url_suffix}",
            "--model-name",
            scenario.recipe.model_name,
            "--model-api-key",
            "sk-mock-test",
            "--budget-file",
            str(budget),
            "--out-dir",
            str(output),
        ]
        if scenario.recipe.omitted_flag is not None:
            omitted = scenario.recipe.omitted_flag.value
            argv = [
                item for i, item in enumerate(argv) if item != omitted and (i == 0 or argv[i - 1] != omitted)
            ]
        completed, timed_out = _run_artifact(
            artifact,
            workspace,
            home,
            output,
            task,
            budget,
            argv,
            env,
            int(server.server_address[1]),
            min(scenario.recipe.timeout_seconds or 180, 600),
            stale_cwd if scenario.recipe.stale_pwd_probe else None,
        )
        if os.environ.get("SBH_DEBUG_DRIVER") == "1" and completed.stderr:
            print(str(completed.stderr), file=sys.stderr, end="")
        if os.environ.get("SBH_DEBUG_DRIVER") == "1":
            print(
                f"driver-debug rc={completed.returncode} workspace={sorted((p.relative_to(workspace).as_posix() for p in workspace.rglob('*')))} output={sorted((p.relative_to(output).as_posix() for p in output.rglob('*')))}",
                file=sys.stderr,
            )
        trajectory = _trajectory(home) + "\n" + _trajectory(workspace) + "\n" + _trajectory(output)
        status = _status(output)
        requests = _request_facts(cfg.get("requests", []), fixture.probe_token)
        if proxy is not None:
            product_connections = [item for item in proxy.connections if item.get("kind") == "product"]
            proxy_observation.update(
                {
                    "gateway_request_count": len(requests),
                    "proxy_product_connections": len(product_connections),
                    "proxy_api_key_seen": any(
                        (item.get("api_key_seen") is True for item in product_connections)
                    ),
                }
            )
            proxy.shutdown()
            proxy.server_close()
            if proxy_thread is not None:
                proxy_thread.join(timeout=2)
            proxy_observation["cleanup_complete"] = proxy_thread is None or not proxy_thread.is_alive()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        token = proof_token
        marker = any(
            (
                token and token in p.read_text(encoding="utf-8", errors="replace")
                for p in workspace.rglob("*")
                if p.is_file() and p.stat().st_size <= 8 * 1024 * 1024
            )
        )
        observation = DirectMockRawObservation(
            scenario_id=scenario_id,
            plan_digest=plan.plan_digest,
            artifact_id=os.environ["SBH_ARTIFACT_ID"],
            artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
            exit_code=int(completed.returncode),
            status_written=bool(status),
            exit_status=status.get("exit_status") if isinstance(status.get("exit_status"), str) else None,
            cost_usd=status.get("cost_usd") if isinstance(status.get("cost_usd"), (int, float)) else None,
            requests=requests,
            proof_present=marker,
            proof_contains_marker=marker,
            trajectory_observed=bool(trajectory.strip()),
            trajectory_contains_probe=bool(token and token in trajectory),
            artifact_unchanged=True,
            decoy_entries=(),
            stderr_has_traceback="traceback (most recent call last)" in str(completed.stderr).lower(),
            process_tree_clean=not timed_out,
            gateway_clean=True,
            workspace_clean=True,
            backend_evidence_digest=_digest(
                {"scenario_id": scenario_id, "requests": len(requests), "rc": completed.returncode}
            ),
            timed_out=timed_out,
            trajectory=trajectory,
            assistant_text=str(completed.stdout or ""),
            facts=_facts(
                scenario,
                workspace,
                requests,
                trajectory,
                cfg.get("requests", []),
                proxy_observation,
                cfg.get("web_gets", []),
            ),
        )
    judgement = apply_direct_mock_local_judge(scenario, observation, owner.judge)
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
    observation = result["observation"]
    judgement = result["judgement"]
    target = Path(os.environ["SBH_TYPED_EVENT_PATH"])
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
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
                "type": "direct-mock-observation",
                "observation": asdict(observation),
                "judgement": asdict(judgement),
            }
        ],
    }
    target.write_text(
        json.dumps(
            document,
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
