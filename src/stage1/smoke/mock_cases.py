"""Mock case library (declarative): use mock_gateway as the "model" to deterministically drive a real
agent (entry.py) into a range of failure and edge scenarios, asserting per case. Zero real-model budget
and parallelisable.

Each case declares:
  id        case name
  mode      mock gateway failure mode (see mock_gateway.build_response)
  command   the bash the agent is told to run (the mock returns it every step; default creates a marker file)
  fixtures  files written into the repo before the run (str or bytes; bytes for dirty-byte cases)
  usage_tokens  the (prompt, completion) token counts of the mock's default usage (for cost cases)
  env       extra environment variables for the entry subprocess (e.g. injecting D2A_MODEL_COST)
  max_steps step budget (small values force a clean LimitsExceeded finish)
  check     assertion function (rc, status, repo, reqs) -> (ok, detail)
  blocking  False = informational (hard to judge strictly black-box; reported but not counted as a pass)

Usage: python mock_cases.py <agent_dir>   (an artifact directory containing a d2a interpreter with litellm)
"""

from __future__ import annotations
import copy
import json
import hashlib
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_gateway as MG
import smoke_contract as SC

NO_TB = "Traceback (most recent call last)"
_SHELL_EDGE_CONTROL_TOKEN = "D2A_SHELL_EDGE_CONTROL_6A31"
_SHELL_EDGE_LEFT_TOKEN = "D2A_SHELL_EDGE_LEFT_6A32"
_SHELL_EDGE_UNTRUNCATED_TOKEN = "D2A_SHELL_EDGE_UNTRUNCATED_6A33"
_SHELL_EDGE_RIGHT_TOKEN = "D2A_SHELL_EDGE_RIGHT_6A34"
_SHELL_EDGE_EXPECTED = (
    f"\n  {_SHELL_EDGE_LEFT_TOKEN}  \n{_SHELL_EDGE_UNTRUNCATED_TOKEN}\n  {_SHELL_EDGE_RIGHT_TOKEN}  \n\n"
)
_SHELL_EDGE_CONTROL_COMMAND = "printf 'D2A_SHELL_EDGE_%s\\n' 'CONTROL_6A31'"
_SHELL_EDGE_TARGET_COMMAND = "printf '\\n  D2A_SHELL_EDGE_%s  \\nD2A_SHELL_EDGE_%s\\n  D2A_SHELL_EDGE_%s  \\n\\n' 'LEFT_6A32' 'UNTRUNCATED_6A33' 'RIGHT_6A34'"
_QGATE_MARK = "QGATE_RAN_7755"
_QGATE_KEYS = ("qualityGate", "beforeFinish", "beforeSubmit", "beforeCompletion")


def _qgate_hooks_json(exit_code: int) -> str:
    cmd = f"sh -c 'echo {_QGATE_MARK} > qgate.marker; exit {exit_code}'"
    body = {k: [{"command": cmd, "action": "run"}] for k in _QGATE_KEYS}
    return json.dumps(body)


def chk_quality_gate_blocks_completion(rc, st, repo, reqs):
    """v6§7:433 quality gate: if the gate does not pass, finishing is NOT allowed -- it must not still record `Submitted`.

    Key design point, and why "not implemented = FAIL" is not used: v6§7:433 says a "must pass tests/lint
    before completing" quality gate MAY be configured, and "may" can be read as "this is an optional
    feature". Failing a build that never implemented a quality gate would break this project's rule
    against failing anything the spec does not mandate.

    So the gate leaves its own trace: the gate command writes `qgate.marker` and then `exit 1`. That
    separates three states naturally:
      - no marker -> this build never ran the gate at all (not implemented / does not recognise these key
        names) -> inconclusive, not a failure;
      - marker present but still recording `Submitted` -> the gate ran, failed, and it still claims a clean
        completion -> FAIL. This does not depend on any reading of "may": v6§9:473 ("Submitted must mean
        what it says") and v6§7:433 ("a failed quality gate forbids finishing") coincide here without
        ambiguity;
      - marker present and `exit_status != Submitted` -> PASS.

    Non-vacuity: `reqs >= 1` prevents "the run never reached the model" being read as "the gate blocked it".
    The paired control `quality_gate_pass_allows_completion` (same hooks.json, gate `exit 0`) proves the
    fixture itself does not prevent completion, ruling out a degenerate implementation that can never
    complete regardless of the gate.
    """
    if not st:
        return (True, f"inconclusive: no status.json written (not judgeable at this layer) rc={rc}")
    if len(reqs) < 1:
        return (
            True,
            'inconclusive: the run produced no model request (reqs=0), so there is no "completion" to speak of',
        )
    ran = (repo / "qgate.marker").exists()
    exit_status = st.get("exit_status")
    if not ran:
        return (
            True,
            f"inconclusive: the quality gate never executed (no {_QGATE_MARK} trace), so this build does not implement it or does not recognise these key names (v6§7:433 says it MAY be configured, so not implementing it is not a failure) exit={exit_status} key names tried={list(_QGATE_KEYS)}",
        )
    ok = exit_status != "Submitted"
    return (
        ok,
        f"the quality gate ran and failed (marker present); exit_status={exit_status!r} must not be Submitted={ok} rc={rc} (v6§7:433 a failed quality gate forbids finishing x v6§9:473 Submitted must mean what it says)",
    )


def chk_quality_gate_blocks_completion_control(rc, st, repo, reqs):
    """Control for the previous cell: same hooks.json with the gate changed to `exit 0`, so it must be able to complete normally.

    Without it, a degenerate implementation that can never complete regardless of the gate would get a free
    PASS on the main cell.
    A gate that never executed is also inconclusive (symmetrical with the main cell, so "not implemented"
    is not read as "the control failed").
    """
    if not st:
        return (True, f"inconclusive: no status.json written rc={rc}")
    if not (repo / "qgate.marker").exists():
        return (
            True,
            f"inconclusive: the quality gate never executed, so this build does not implement it and the control does not apply exit={st.get('exit_status')}",
        )
    ok = st.get("exit_status") == "Submitted" and rc == 0
    return (
        ok,
        f"with the gate passing (exit 0) it must complete normally: exit_status={st.get('exit_status')!r} rc={rc} ok={ok} (control: proves the fixture itself does not prevent completion)",
    )


def _shell_edge_results(reqs: list[dict], marker: str) -> list[str]:
    """Return only tool results containing an output-only marker."""
    return [text for request in reqs for text in request.get("tool_result_texts") or [] if marker in text]


def chk_shell_output_edge_whitespace_control(rc, st, repo, reqs):
    matches = _shell_edge_results(reqs, _SHELL_EDGE_CONTROL_TOKEN)
    live = rc == 0 and len(reqs) >= 2 and (st.get("exit_status") in ("Submitted", "LimitsExceeded"))
    ok = live and bool(matches)
    return (
        ok,
        f"ordinary_line_roundtrip={bool(matches)} matches={len(matches)} reqs={len(reqs)} rc={rc} exit={st.get('exit_status')} (the complete control token can only be assembled by real printf stdout; a command echo cannot compensate)",
    )


def chk_shell_output_edge_whitespace(rc, st, repo, reqs):
    matches = _shell_edge_results(reqs, _SHELL_EDGE_UNTRUNCATED_TOKEN)
    expected = _SHELL_EDGE_EXPECTED.encode("ascii")
    exact = [expected in text.encode("utf-8") for text in matches]
    live = rc == 0 and len(reqs) >= 2 and (st.get("exit_status") in ("Submitted", "LimitsExceeded"))
    ok = live and bool(matches) and any(exact)
    return (
        ok,
        f"edge_bytes_exact={any(exact)} marker_results={len(matches)} reqs={len(reqs)} rc={rc} exit={st.get('exit_status')} (the leading/trailing blank lines and the spaces around the content line of a short untruncated stdout must be present verbatim as contiguous ASCII bytes in the matching tool result; strip/rstrip/lstrip and whitespace normalisation all FAIL)",
    )


def chk_secret_not_leaked(rc, st, repo, reqs):
    leaked = any((r.get("has_probe") for r in reqs))
    ok = not leaked and len(reqs) >= 1 and (st.get("exit_status") in ("Submitted", "LimitsExceeded"))
    return (
        ok,
        f"un-@-referenced private key sentinel in a request body={leaked} reqs={len(reqs)} exit={st.get('exit_status')} (a filename mentioned in free text must not trigger an auto-read and leak; the sentinel in a request body is a private key leak)",
    )


def chk_probe_in_body(rc, st, repo, reqs):
    seen = any((r.get("has_probe") for r in reqs))
    ok = seen and st.get("exit_status") in ("Submitted", "LimitsExceeded")
    return (
        ok,
        f"sentinel of the read file in a request body={seen} reqs={len(reqs)} exit={st.get('exit_status')} (positive control: content really cat-ed should be fed back onto the wire, proving the leak probe works)",
    )
_PEER_WEB_URL_PLACEHOLDER = "__PEER_WEBFETCH_URL__"
_PEER_WEB_CONTROL_TOKEN = "PEER_WEBFETCH_CONTROL_BODY_7528"
_PEER_WEB_BODY_TOKENS = {
    404: "PEER_WEBFETCH_BODY_ALPHA_8639",
    429: "PEER_WEBFETCH_BODY_BETA_9741",
    500: "PEER_WEBFETCH_BODY_GAMMA_1852",
}


def _peer_web_rounds() -> list[list[dict]]:
    return [
        [
            {
                "name": ["web_fetch", "web-fetch", "webfetch", "WebFetch"],
                "args": {"url": _PEER_WEB_URL_PLACEHOLDER},
            }
        ],
        [{"name": "__finish__"}],
    ]


def _peer_exchange(reqs: list[dict]) -> dict:
    projections = [
        dict(req.get("peer_exchange") or {})
        for req in reqs or []
        if (req.get("peer_exchange") or {}).get("target_call_seen")
    ]
    paired = [row for row in projections if row.get("target_result_seen")]
    return paired[-1] if paired else projections[-1] if projections else {}


def _peer_web_gateway(st: dict) -> dict:
    return dict((st or {}).get("_gateway") or {})


def chk_webfetch_http_failure_control(rc, st, repo, reqs):
    """Zero-weight: a real local HTTP 200 must reach web_fetch and its matching result."""
    gateway = _peer_web_gateway(st)
    gets = list(gateway.get("web_gets") or [])
    exchange = _peer_exchange(reqs)
    text = str(exchange.get("target_result_text") or "")
    first_tools = (reqs[0] if reqs else {}).get("tool_names") or []
    facts = {
        "web_fetch_advertised": any((MG.canon_tool(name) == "web_fetch" for name in first_tools)),
        "one_http_get": len(gets) == 1,
        "http_200_sent": len(gets) == 1
        and gets[0].get("status") == 200
        and (gets[0].get("send_complete") is True),
        "one_target_call": exchange.get("target_call_count") == 1,
        "one_matching_result": exchange.get("target_result_count") == 1,
        "id_paired": exchange.get("id_paired") is True,
        "body_public": _PEER_WEB_CONTROL_TOKEN in text,
        "clean_process": rc == 0
        and st.get("exit_status") in ("Submitted", "LimitsExceeded")
        and (not st.get("_timed_out")),
        "gateway_cleanup": gateway.get("cleanup_complete") is True,
    }
    ok = all(facts.values())
    return (
        ok,
        f"webfetch_http_control={ok} facts={facts} reqs={len(reqs)} result_sha={exchange.get('target_result_sha256')} (the local 200 body must be exposed via a real GET and a role=tool result with a matching call-id; exposing only the schema, only hitting HTTP, or compensating across calls does not open the target gate)",
    )


def _peer_web_failure_marked(text: str) -> bool:
    low = text.lower()
    structured = any(
        (
            re.search(pattern, low)
            for pattern in (
                "[\"\\']?(?:ok|success)[\"\\']?\\s*[:=]\\s*false",
                "[\"\\']?is[_-]?error[\"\\']?\\s*[:=]\\s*true",
                "[\"\\']?status[\"\\']?\\s*[:=]\\s*[\"\\'](?:error|failed|failure)",
            )
        )
    )
    verbal = any(
        (
            sig in low
            for sig in (
                "error",
                "failed",
                "failure",
                "unsuccessful",
                "not successful",
                "non-success",
                "not found",
                "too many requests",
                "rate limit",
                "server error",
                "internal server",
                "service unavailable",
                "bad gateway",
            )
        )
    )
    return structured or verbal


def _peer_web_status_preserved(text: str, status_code: int) -> bool:
    return bool(
        re.search(f"(?:http|status(?:\\s+code)?|code|->)[^0-9]{{0,18}}{status_code}(?:\\D|$)", text, re.I)
    )


def chk_webfetch_http_failure_matrix(rows: list[dict]):
    expected = {"http_404": 404, "http_429": 429, "http_500": 500}
    labelled = {row.get("label"): row for row in rows}
    if set(labelled) != set(expected):
        return (False, f"runner error: webfetch variants={sorted(labelled)} expected={sorted(expected)}")
    all_facts = {}
    for label, status_code in expected.items():
        row = labelled[label]
        st, reqs = (row.get("status") or {}, row.get("reqs") or [])
        gateway = _peer_web_gateway(st)
        gets = list(gateway.get("web_gets") or [])
        exchange = _peer_exchange(reqs)
        text = str(exchange.get("target_result_text") or "")
        low = text.lower()
        explicit_success = bool(re.search("[\"\\']?(?:ok|success)[\"\\']?\\s*[:=]\\s*true", low))
        facts = {
            "bounded_gets": 1 <= len(gets) <= 3,
            "exact_http_status": bool(gets) and all((get.get("status") == status_code for get in gets)),
            "response_complete": bool(gets) and all((get.get("send_complete") is True for get in gets)),
            "one_target_call": exchange.get("target_call_count") == 1,
            "one_matching_result": exchange.get("target_result_count") == 1,
            "id_paired": exchange.get("id_paired") is True,
            "status_preserved": _peer_web_status_preserved(text, status_code),
            "failure_marked": _peer_web_failure_marked(text),
            "not_explicit_success": not explicit_success,
            "clean_process": row.get("rc") == 0
            and st.get("exit_status") in ("Submitted", "LimitsExceeded")
            and (not st.get("_timed_out")),
            "gateway_cleanup": gateway.get("cleanup_complete") is True,
        }
        all_facts[label] = {
            "ok": all(facts.values()),
            "facts": facts,
            "result_sha256": exchange.get("target_result_sha256"),
            "result_preview": text[:180],
        }
    ok = all((row["ok"] for row in all_facts.values()))
    return (
        ok,
        f"webfetch_http_failure={ok} variants={all_facts} (404/429/500 must each preserve the exact status in the public tool result with the matching call-id and be clearly marked as failed; treating `GET ... -> HTTP N` plus the body as ordinary successful output is a FAIL)",
    )


_EGRESS_PROXY_FINAL = "EGRESS_PROXY_DIRECT_FINAL_1753"


def _egress_gateway(st: dict) -> dict:
    return dict((st or {}).get("_gateway") or {})


def _egress_proxy_observation(st: dict) -> dict:
    return dict((st or {}).get("_egress_proxy") or {})


def _egress_proxy_facts(rc, st, reqs, *, injected: bool) -> dict:
    obs = _egress_proxy_observation(st)
    gateway = _egress_gateway(st)
    return {
        "gateway_self_check": obs.get("gateway_self_check") is True,
        "proxy_self_check": obs.get("proxy_self_check") is True,
        "environment_exact": obs.get("environment_exact") is True and obs.get("proxy_injected") is injected,
        "nonlocalhost_explicit_host": obs.get("gateway_host") == "127.0.0.2",
        "one_gateway_request": len(reqs) == 1 and gateway.get("request_count") == 1,
        "gateway_response_complete": gateway.get("response_statuses") == [200]
        and gateway.get("response_send_complete") == [True],
        "zero_proxy_connections": obs.get("proxy_product_connections") == 0,
        "zero_proxy_api_key": obs.get("proxy_api_key_seen") is False,
        "clean_terminal": rc == 0
        and st.get("exit_status") in ("Submitted", "LimitsExceeded")
        and (not st.get("_timed_out")),
        "cleanup": obs.get("cleanup_complete") is True and gateway.get("cleanup_complete") is True,
    }


def chk_http_proxy_hermetic_control(rc, st, repo, reqs):
    """Zero-weight: with every ambient proxy unset, the explicit gateway closes once."""
    facts = _egress_proxy_facts(rc, st, reqs, injected=False)
    ok = all(facts.values())
    return (
        ok,
        f"http_proxy_control={ok} facts={facts} (both the explicit 127.0.0.2 D2A endpoint and the trap proxy pass a raw self-check first; with no proxy environment the single model request may only enter the gateway ledger)",
    )


def chk_http_proxy_hermetic(rc, st, repo, reqs):
    facts = _egress_proxy_facts(rc, st, reqs, injected=True)
    ok = all(facts.values())
    return (
        ok,
        f"http_proxy_hermetic={ok} facts={facts} (when HTTP_PROXY/HTTPS_PROXY/ALL_PROXY and their case variants point at the self-checked trap, the explicit D2A model endpoint must still own the only request, with zero proxy connections and zero API keys seen there)",
    )


def chk_artifact_has(rel, spec, *, nonempty=False, glob_in=""):
    """The v6§0.1.1 artifact root manifest: a given convention file/directory must be present in the artifact.

    Static check; the signature differs from a normal mock check -- it takes `(agent_dir)` and is called
    from `_run_one`'s `static_artifact` branch (no mock server, no agent run).

    Rationale: spec items 1/6/7 require these files. The build-time contract check is a GATE, not a
    test point, and does not look at pyproject.toml or install.sh at all.

    `glob_in` covers shapes like `wheels/` where the directory must contain at least one .whl.
    """

    def _f(agent_dir):
        p = Path(agent_dir) / rel
        if glob_in:
            hits = sorted(p.glob(glob_in)) if p.is_dir() else []
            return (
                bool(hits),
                f"{rel} directory exists={p.is_dir()} containing {len(hits)} {glob_in} ({spec})",
            )
        exists = p.is_file()
        if exists and nonempty:
            size = p.stat().st_size
            return (size > 0, f"{rel} exists but size={size}B (must be non-empty) ({spec})")
        return (exists, f"{rel} exists={exists} ({spec})")

    return _f


def chk_artifact_declares_deps(spec):
    """v6§0.1.1:25: new third-party dependencies must be declared in `[project.dependencies]` of
    `pyproject.toml` (OR in `requirements.txt`).

    The spec offers a choice of two, so the check must too -- accepting only pyproject.toml would
    wrongly fail a compliant requirements.txt implementation. It fails only when neither is present.
    """

    def _f(agent_dir):
        d = Path(agent_dir)
        pj, rt = (d / "pyproject.toml", d / "requirements.txt")
        has_pj = pj.is_file() and "[project" in pj.read_text(encoding="utf-8", errors="replace")
        has_rt = rt.is_file() and rt.stat().st_size > 0
        return (
            has_pj or has_rt,
            f"pyproject.toml has [project...]={has_pj} requirements.txt non-empty={has_rt} (either is sufficient) ({spec})",
        )

    return _f


CASES = [
    dict(
        id="artifact_has_run_agent_sh",
        static_artifact=True,
        blocking=False,
        check=chk_artifact_has("run_agent.sh", "v6§0.1.1:20 requires `run_agent.sh` in the artifact root"),
    ),
    dict(
        id="artifact_has_entry_py",
        static_artifact=True,
        blocking=False,
        check=chk_artifact_has("entry.py", "v6§0.1.1:20 requires `entry.py` in the artifact root"),
    ),
    dict(
        id="artifact_has_offline_wheel",
        static_artifact=True,
        blocking=False,
        check=chk_artifact_has(
            "wheels", "v6§0.1.1:20 `wheels/` (at least one offline wheel)", glob_in="*.whl"
        ),
    ),
    dict(
        id="artifact_has_requirements_lock",
        static_artifact=True,
        blocking=False,
        check=chk_artifact_has(
            "requirements.lock",
            "v6§0.1.1:20 requires `requirements.lock` in the artifact root",
            nonempty=True,
        ),
    ),
    dict(
        id="artifact_has_version_file",
        static_artifact=True,
        blocking=False,
        check=chk_artifact_has(
            "VERSION", "v6§0.1.1:20 requires `VERSION` in the artifact root", nonempty=True
        ),
    ),
    dict(
        id="artifact_declares_dependencies",
        static_artifact=True,
        blocking=False,
        check=chk_artifact_declares_deps(
            "v6§0.1.1:25: new third-party dependencies must be declared in `[project.dependencies]` of `pyproject.toml` (or in `requirements.txt`)"
        ),
    ),
    dict(
        id="artifact_has_install_sh",
        static_artifact=True,
        blocking=False,
        check=chk_artifact_has(
            "install.sh",
            "v6§0.1.1:26 `install.sh` installs everything in one step -- this cell only judges that it exists; whether it installs cleanly is a higher bar",
        ),
    ),
    dict(
        id="quality_gate_blocks_completion",
        mode="seq_probe",
        max_steps=4,
        rounds=[[{"name": "bash", "command": "echo QGATE_EDIT_7755 > edited.txt"}]],
        fixtures={"hooks.json": _qgate_hooks_json(1)},
        gate="quality_gate_blocks_completion_control",
        check=chk_quality_gate_blocks_completion,
        blocking=True,
        timeout=90,
    ),
    dict(
        id="quality_gate_blocks_completion_control",
        mode="seq_probe",
        max_steps=4,
        rounds=[[{"name": "bash", "command": "echo QGATE_EDIT_7755 > edited.txt"}]],
        fixtures={"hooks.json": _qgate_hooks_json(0)},
        check=chk_quality_gate_blocks_completion_control,
        blocking=False,
        timeout=90,
    ),
    dict(
        id="unreferenced_secret_not_in_request_body",
        mode="normal",
        max_steps=2,
        task="Look into the permission problem on ~/.ssh/id_rsa; the format notes in secret.key may help. Contact alice@example.com.",
        fixtures={
            "secret.key": "-----BEGIN OPENSSH PRIVATE KEY-----\nSK_SENTINEL_4242\n-----END OPENSSH PRIVATE KEY-----\n"
        },
        probe_token="SK_SENTINEL_4242",
        gate="secret_probe_mechanism_control",
        check=chk_secret_not_leaked,
        blocking=True,
    ),
    dict(
        id="secret_probe_mechanism_control",
        mode="normal",
        max_steps=2,
        command="cat ref.txt",
        fixtures={"ref.txt": "REFTOK_7731 content body\n"},
        probe_token="REFTOK_7731",
        check=chk_probe_in_body,
        blocking=False,
    ),
    dict(
        id="exfil_glob_pem_not_leaked",
        mode="normal",
        max_steps=2,
        task="Audit every *.pem certificate file in the repository for imminent expiry and state your conclusion.",
        fixtures={
            "server.pem": "-----BEGIN CERTIFICATE-----\nPEM_SENTINEL_5583\n-----END CERTIFICATE-----\n"
        },
        probe_token="PEM_SENTINEL_5583",
        gate="secret_probe_mechanism_control",
        check=chk_secret_not_leaked,
        blocking=True,
    ),
    dict(
        id="webfetch_http_failure_control",
        mode="seq_probe",
        rounds=_peer_web_rounds(),
        max_steps=3,
        timeout=45,
        allow_network=True,
        case_local_home=True,
        record_responses=True,
        process_group_cleanup=True,
        peer_web_status=200,
        peer_web_body=_PEER_WEB_CONTROL_TOKEN,
        peer_web_path="/peer-webfetch/control",
        wire_tool_capability="web_fetch",
        env={
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
        check=chk_webfetch_http_failure_control,
        blocking=False,
    ),
    dict(
        id="webfetch_http_failure",
        mode="seq_probe",
        rounds=_peer_web_rounds(),
        max_steps=3,
        timeout=45,
        allow_network=True,
        case_local_home=True,
        record_responses=True,
        process_group_cleanup=True,
        wire_tool_capability="web_fetch",
        env={
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        },
        variants=[
            dict(
                label="http_404",
                peer_web_status=404,
                peer_web_body=_PEER_WEB_BODY_TOKENS[404],
                peer_web_path="/peer-webfetch/alpha",
            ),
            dict(
                label="http_429",
                peer_web_status=429,
                peer_web_body=_PEER_WEB_BODY_TOKENS[429],
                peer_web_path="/peer-webfetch/beta",
            ),
            dict(
                label="http_500",
                peer_web_status=500,
                peer_web_body=_PEER_WEB_BODY_TOKENS[500],
                peer_web_path="/peer-webfetch/gamma",
            ),
        ],
        gate="webfetch_http_failure_control",
        check_variants=chk_webfetch_http_failure_matrix,
        blocking=True,
    ),
    dict(
        id="http_proxy_hermetic_control",
        egress_proxy=True,
        egress_proxy_inject=False,
        mode="fixed_reply",
        reply_content=_EGRESS_PROXY_FINAL,
        max_steps=2,
        timeout=30,
        case_local_home=True,
        process_group_cleanup=True,
        record_responses=True,
        check=chk_http_proxy_hermetic_control,
        blocking=False,
    ),
    dict(
        id="http_proxy_hermetic",
        egress_proxy=True,
        egress_proxy_inject=True,
        mode="fixed_reply",
        reply_content=_EGRESS_PROXY_FINAL,
        max_steps=2,
        timeout=30,
        case_local_home=True,
        process_group_cleanup=True,
        record_responses=True,
        gate="http_proxy_hermetic_control",
        check=chk_http_proxy_hermetic,
        blocking=True,
    ),
    dict(
        id="shell_output_edge_whitespace_control",
        mode="seq_probe",
        max_steps=3,
        timeout=45,
        case_local_home=True,
        rounds=[[{"name": "bash", "command": _SHELL_EDGE_CONTROL_COMMAND}]],
        check=chk_shell_output_edge_whitespace_control,
        blocking=False,
    ),
    dict(
        id="shell_output_edge_whitespace",
        mode="seq_probe",
        max_steps=3,
        timeout=45,
        case_local_home=True,
        rounds=[[{"name": "bash", "command": _SHELL_EDGE_TARGET_COMMAND}]],
        gate="shell_output_edge_whitespace_control",
        check=chk_shell_output_edge_whitespace,
        blocking=True,
    ),
]
_MODEL_ARGV_NAMES = ("--model-base-url", "--model-name", "--model-api-key")


def _rejects_model_argv(stderr: str) -> bool:
    """Whether stderr is complaining that it does not recognise the three model arguments (covers argparse, click and hand-written branches).

    Both conditions are required on purpose (wording about an unknown/unrecognised argument AND a
    mention of a model argument name) so that an rc=2 a case deliberately provokes (malformed
    task-file, illegal tier) is not mistaken for a wrong call form and retried away.
    """
    low = (stderr or "").lower()
    complains = any(
        (
            sig in low
            for sig in (
                "unrecognized argument",
                "unrecognized arguments",
                "unknown argument",
                "unknown option",
                "no such option",
                "unexpected extra argument",
                "invalid option",
            )
        )
    )
    return complains and any((name in low for name in _MODEL_ARGV_NAMES))


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_process_group(pgid: int) -> bool:
    if not _process_group_exists(pgid):
        return True
    for sig, grace in ((signal.SIGTERM, 0.25), (signal.SIGKILL, 0.75)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _process_group_exists(pgid):
                return True
            time.sleep(0.01)
    return not _process_group_exists(pgid)


def _run_captured(argv: list[str], *, cwd: Path, env: dict, timeout: float, process_group: bool) -> dict:
    """Run one public entry invocation; deadline cases additionally census/clean its owned process group."""
    if not process_group:
        try:
            result = subprocess.run(
                argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env
            )
            return {
                "rc": result.returncode,
                "stderr": result.stderr or "",
                "stdout": result.stdout or "",
                "timed_out": False,
                "cleanup_complete": True,
                "residual_process_observed": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "rc": -1,
                "stderr": "TIMEOUT",
                "stdout": "",
                "timed_out": True,
                "cleanup_complete": True,
                "residual_process_observed": False,
            }
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        close_fds=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=0.25)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=0.75)
            except subprocess.TimeoutExpired:
                stdout, stderr = ("", "TIMEOUT")
    residual = _process_group_exists(process.pid)
    cleanup_complete = _cleanup_process_group(process.pid) if residual else True
    return {
        "rc": -1 if timed_out else process.returncode,
        "stderr": "TIMEOUT" if timed_out else stderr or "",
        "stdout": stdout or "",
        "timed_out": timed_out,
        "cleanup_complete": cleanup_complete,
        "residual_process_observed": residual,
    }


def _run_entry(agent_dir: Path, base_url: str, case: dict):
    sandbox = Path(tempfile.mkdtemp(prefix=f"mc_{case['id']}_box_"))
    repo = sandbox / "repo"
    repo.mkdir()
    out = Path(tempfile.mkdtemp(prefix=f"mc_{case['id']}_out_"))
    launch_cwd = agent_dir
    for fn, content in (case.get("fixtures") or {}).items():
        p = repo / fn
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
    task_payload = {
        "task_id": "d2a-mock-task",
        "type": "swe",
        "problem_statement": case.get("task", "do the task"),
        "repo_dir": str(repo),
        "allow_network": bool(case.get("allow_network", False)),
    }
    (repo / "_task.json").write_text(json.dumps(task_payload))
    budget_payload = {
        "max_steps": case.get("max_steps", 2),
        "max_tokens": 0,
        "max_wall_seconds": 1800,
    }
    (repo / "_budget.json").write_text(json.dumps(budget_payload))
    env = dict(os.environ)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["MSWEA_COST_TRACKING"] = "ignore_errors"
    env.update(case.get("env") or {})
    env["D2A_MODEL_BASE_URL"] = base_url
    env["D2A_MODEL_NAME"] = "litellm_proxy/mock"
    env["D2A_MODEL_API_KEY"] = "sk-mock-test"
    if case.get("case_local_home"):
        local_home = sandbox / "home"
        local_home.mkdir()
        env["HOME"] = str(local_home)
        env["YUYAO_HOME"] = str(local_home)
        env["XDG_CONFIG_HOME"] = str(sandbox / "xdg-config")
        env["XDG_DATA_HOME"] = str(sandbox / "xdg-data")
        env["XDG_CACHE_HOME"] = str(sandbox / "xdg-cache")
    model_name = "litellm_proxy/mock"
    model_argv = ["--model-base-url", base_url, "--model-name", model_name, "--model-api-key", "sk-mock-test"]
    core_argv = [
        "--task-file",
        str(repo / "_task.json"),
        "--repo-dir",
        str(repo),
        "--budget-file",
        str(repo / "_budget.json"),
        "--out-dir",
        str(out),
    ]
    cmd = [sys.executable, "entry.py", *core_argv, *model_argv]
    env_only_cmd = [sys.executable, "entry.py", *core_argv]
    env_only_extra = {
        "D2A_MODEL_BASE_URL": base_url,
        "D2A_MODEL_NAME": model_name,
        "D2A_MODEL_API_KEY": "sk-mock-test",
    }
    budget = case.get("timeout", 90)
    started = time.monotonic()
    process_group = bool(case.get("process_group_cleanup"))
    run = _run_captured(cmd, cwd=launch_cwd, env=env, timeout=budget, process_group=process_group)
    if run["rc"] == 2 and True and _rejects_model_argv(run["stderr"]):
        env2 = {**env, **env_only_extra}
        retry = _run_captured(
            env_only_cmd, cwd=launch_cwd, env=env2, timeout=budget, process_group=process_group
        )
        retry["cleanup_complete"] = retry["cleanup_complete"] and run["cleanup_complete"]
        retry["residual_process_observed"] = (
            retry["residual_process_observed"] or run["residual_process_observed"]
        )
        run = retry
    rc, stderr, stdout_cap = (run["rc"], run["stderr"], run["stdout"])
    timed_out = run["timed_out"]
    process_ended_ns = time.monotonic_ns()
    elapsed_s = round(time.monotonic() - started, 1)
    status = {}
    sf = out / "status.json"
    if sf.exists():
        try:
            status = json.loads(sf.read_text())
        except Exception:
            status = {"_parse_error": True}
    if timed_out:
        status = status or {}
        status["_timed_out"] = True
    traj = ""
    tj = out / "trajectory.json"
    if tj.exists():
        try:
            traj += tj.read_text(errors="replace")
        except Exception:
            pass
    tdir = out / ".trajectory"
    if tdir.exists():
        for p in sorted(tdir.rglob("*")):
            if not p.is_file():
                continue
            try:
                traj += p.read_text(errors="replace")
            except Exception:
                pass
    asst = []
    if tj.exists():
        try:
            tjd = json.loads(tj.read_text(errors="replace"))
            msgs = tjd.get("messages") if isinstance(tjd, dict) else tjd if isinstance(tjd, list) else []
            for m in msgs or []:
                if isinstance(m, dict) and m.get("role") == "assistant":
                    c = m.get("content")
                    asst.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
        except Exception:
            pass
    if status is not None:
        status = status if isinstance(status, dict) else {}
        status["_trajectory"] = traj
        status["_assistant_texts"] = "\n".join(asst)
        status["_repo_parent"] = str(sandbox)
        status["_stdout"] = stdout_cap
        status["_elapsed_s"] = elapsed_s
        status["_budget_s"] = budget
        if process_group:
            status["_cleanup_complete"] = run["cleanup_complete"]
            status["_residual_process_observed"] = run["residual_process_observed"]
            status["_process_ended_ns"] = process_ended_ns
    return (rc, stderr, status, repo)


def _meter_number_shape(value) -> str:
    if type(value) is bool:
        return "bool"
    if type(value) is int:
        return "negative-int" if value < 0 else "int"
    if type(value) is float:
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "infinity"
        return "negative-float" if value < 0 else "float"
    if type(value) is str:
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


def _meter_usage_shape(body: object) -> dict:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {
            "usage": (
                "missing" if not isinstance(body, dict) or "usage" not in body else _meter_number_shape(usage)
            )
        }
    return {
        key: _meter_number_shape(usage.get(key))
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }


def _meter_usage_value(body: object) -> dict:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


def _response_acceptance_shape(body: object) -> str:
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list) or (not body["choices"]):
        return "missing_choices"
    choice = body["choices"][0]
    if not isinstance(choice, dict) or "message" not in choice:
        return "missing_message"
    message = choice.get("message")
    if not isinstance(message, dict):
        return "invalid_message"
    if choice.get("finish_reason") == "tool_calls" and message.get("tool_calls") == []:
        return "tool_calls_empty"
    return "valid"


def _peer_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            (str(part.get("text") or part.get("content") or "") for part in content if isinstance(part, dict))
        )
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


def _peer_tool_matches(name: object, cfg: dict) -> bool:
    value = str(name or "")
    if value in set(cfg.get("wire_tool_names") or ()):
        return True
    capability = str(cfg.get("wire_tool_capability") or "")
    return bool(capability) and MG.canon_tool(value) == capability


def _peer_exchange_projection(raw: bytes, cfg: dict) -> dict:
    """Project one configured tool exchange, retaining only its bounded public result."""
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        body = {}
    messages = body.get("messages") if isinstance(body, dict) else []
    messages = messages if isinstance(messages, list) else []
    advertised = []
    for tool in body.get("tools") or [] if isinstance(body, dict) else []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if isinstance(function, dict) and _peer_tool_matches(function.get("name"), cfg):
            advertised.append(str(function.get("name") or ""))
    calls = []
    results = []
    pairs = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict) or not _peer_tool_matches(function.get("name"), cfg):
                continue
            call_id = call.get("id")
            calls.append(
                {"id": call_id, "name": str(function.get("name") or ""), "message_index": message_index}
            )
            for result in messages[message_index + 1 :]:
                if not isinstance(result, dict):
                    continue
                if result.get("role") == "assistant":
                    break
                if result.get("role") != "tool":
                    continue
                result_id = result.get("tool_call_id")
                if call_id is not None and result_id != call_id:
                    continue
                text = _peer_content_text(result.get("content"))
                projected = {
                    "id": result_id,
                    "text": text,
                    "bytes": len(text.encode("utf-8", errors="replace")),
                }
                results.append(projected)
                pairs.append((calls[-1], projected))
                break
    selected_call = pairs[-1][0] if pairs else calls[-1] if calls else {}
    selected_result = pairs[-1][1] if pairs else {}
    result_text = str(selected_result.get("text") or "")
    return {
        "target_advertised": advertised,
        "target_call_seen": bool(calls),
        "target_call_count": len(calls),
        "target_call_name": selected_call.get("name"),
        "target_call_id": selected_call.get("id"),
        "target_result_seen": bool(results),
        "target_result_count": len(results),
        "target_result_id": selected_result.get("id"),
        "id_paired": bool(pairs)
        and selected_call.get("id") is not None
        and (selected_call.get("id") == selected_result.get("id")),
        "target_result_bytes": selected_result.get("bytes", 0),
        "target_result_sha256": (
            hashlib.sha256(result_text.encode("utf-8", errors="replace")).hexdigest() if result_text else ""
        ),
        "target_result_text": result_text[:8000],
    }


class _MeterHandler(MG.Handler):
    """Add response receipts, Retry-After, and a bounded blocking peer around the existing gateway."""

    def do_POST(self):
        cfg = getattr(self.server, "cfg", MG.CONFIG)
        cfg.setdefault("_request_times_ns", []).append(time.monotonic_ns())
        if not (cfg.get("wire_tool_names") or cfg.get("wire_tool_capability")):
            return super().do_POST()
        before = len(cfg.get("requests") or [])
        original = self.rfile
        capture = _TurnCaptureReader(original)
        self.rfile = capture
        try:
            return super().do_POST()
        finally:
            self.rfile = original
            projection = _peer_exchange_projection(bytes(capture.data), cfg)
            lock = cfg.setdefault("_peer_wire_lock", threading.Lock())
            with lock:
                requests = cfg.get("requests") or []
                if before < len(requests):
                    requests[before]["peer_exchange"] = projection

    def do_GET(self):
        cfg = getattr(self.server, "cfg", MG.CONFIG)
        target_path = str(cfg.get("peer_web_path") or "")
        if not target_path or not self.path.startswith(target_path):
            return super().do_GET()
        status = int(cfg.get("peer_web_status") or 200)
        body = str(cfg.get("peer_web_body") or "").encode("utf-8")
        record = {"path": self.path, "status": status, "body_bytes": len(body), "send_complete": False}
        cfg.setdefault("_peer_web_gets", []).append(record)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            record["send_complete"] = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            record["peer_closed"] = True
        finally:
            self.close_connection = True

    def _send_json_with_retry_after(self, status: int, body: dict, retry_after: str) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Retry-After", retry_after)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def _send_blocking(self, status: int, body: dict, record: dict, cfg: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        release = cfg["_block_release_event"]
        done = cfg["_block_handler_done"]
        sent = False
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data[:1])
            self.wfile.flush()
            record["block_entered_ns"] = time.monotonic_ns()
            release.wait(float(cfg.get("block_response_s") or 0.0))
            record["block_released_ns"] = time.monotonic_ns()
            self.wfile.write(data[1:])
            self.wfile.flush()
            sent = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            record["peer_closed"] = True
        finally:
            record["send_complete"] = sent
            self.close_connection = True
            done.set()

    def _send(self, status: int, body: dict):
        cfg = getattr(self.server, "cfg", MG.CONFIG)
        if not cfg.get("record_responses") or self.command != "POST":
            return super()._send(status, body)
        if status == 200 and cfg.get("response_mutation") == "missing_message":
            body = copy.deepcopy(body)
            choices = body.get("choices") if isinstance(body, dict) else None
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choices[0].pop("message", None)
        attempt = len(cfg.get("requests") or [])
        retry_after = None
        if attempt == 1 and status in (429, 503) and (cfg.get("retry_after") is not None):
            retry_after = str(cfg["retry_after"])
        record = {
            "attempt": attempt,
            "status": status,
            "retry_after": retry_after,
            "usage_shape": _meter_usage_shape(body),
            "usage_value": _meter_usage_value(body),
            "acceptance_shape": _response_acceptance_shape(body),
            "send_complete": False,
            "block_entered_ns": None,
            "block_released_ns": None,
            "peer_closed": False,
        }
        cfg.setdefault("_response_facts", []).append(record)
        if attempt == 1 and status == 200 and cfg.get("block_response_s"):
            self._send_blocking(status, body, record, cfg)
            return
        try:
            if retry_after is None:
                super()._send(status, body)
            else:
                self._send_json_with_retry_after(status, body, retry_after)
            record["send_complete"] = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            record["peer_closed"] = True
            raise


class _MeterServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class _TurnCaptureReader:
    """Tee the one Content-Length read performed by the existing gateway handler."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.data = bytearray()

    def read(self, size=-1):
        chunk = self.wrapped.read(size)
        if chunk:
            self.data.extend(chunk)
        return chunk

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class _EgressProxyGatewayHandler(_MeterHandler):
    """Explicit model gateway with an out-of-ledger raw health endpoint."""

    def do_GET(self):
        cfg = getattr(self.server, "cfg", MG.CONFIG)
        if self.path.startswith("/egress-self-check/"):
            cfg.setdefault("_egress_gateway_self_checks", []).append(self.path)
            self._send_raw(200, b'{"ok":true}', "application/json")
            return
        return _MeterHandler.do_GET(self)


class _EgressProxyHandler(MG.Handler):
    """Raw trap proxy: every parsed request is a product-visible reroute."""

    def _trap(self) -> None:
        cfg = getattr(self.server, "cfg", {})
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if 0 < length <= 2 * 1024 * 1024 else b""
        authorization = str(self.headers.get("Authorization") or "")
        kind = "self-check" if self.headers.get("X-Egress-Self") == "proxy-self-check" else "product"
        record = {
            "ordinal": len(cfg.get("_egress_proxy_connections") or []),
            "kind": kind,
            "method": self.command,
            "target": self.path,
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "api_key_seen": "sk-mock-test" in authorization,
        }
        with cfg["_egress_proxy_lock"]:
            cfg.setdefault("_egress_proxy_connections", []).append(record)
        self._send_raw(502, b'{"proxy":"egress-trap"}', "application/json")

    do_CONNECT = _trap
    do_DELETE = _trap
    do_GET = _trap
    do_HEAD = _trap
    do_OPTIONS = _trap
    do_PATCH = _trap
    do_POST = _trap
    do_PUT = _trap


def _egress_raw_http(host: str, port: int, request: bytes) -> bytes:
    response = bytearray()
    with socket.create_connection((host, port), timeout=2.0) as connection:
        connection.settimeout(2.0)
        connection.sendall(request)
        while len(response) <= 65536:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
    return bytes(response)


def _execute_egress_proxy_case(agent_dir: Path, case: dict):
    cfg = _gateway_cfg(case)
    gateway_server = _MeterServer(("127.0.0.2", 0), _EgressProxyGatewayHandler)
    gateway_server.cfg = cfg
    gateway_thread = threading.Thread(target=gateway_server.serve_forever, daemon=True)
    proxy_cfg = {"_egress_proxy_connections": [], "_egress_proxy_lock": threading.Lock()}
    proxy_server = _MeterServer(("127.0.0.1", 0), _EgressProxyHandler)
    proxy_server.cfg = proxy_cfg
    proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
    gateway_thread.start()
    proxy_thread.start()
    gateway_host, gateway_port = gateway_server.server_address
    proxy_host, proxy_port = proxy_server.server_address
    gateway_check_path = "/egress-self-check/gateway"
    gateway_self_check = False
    proxy_self_check = False
    status = {}
    repo = Path(tempfile.mkdtemp(prefix="mc_egress_proxy_unstarted_"))
    rc, stderr = (-1, "")
    try:
        gateway_response = _egress_raw_http(
            gateway_host,
            gateway_port,
            f"GET {gateway_check_path} HTTP/1.1\r\nHost: {gateway_host}:{gateway_port}\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            ),
        )
        gateway_self_check = gateway_response.startswith(b"HTTP/1.1 200 ") and cfg.get(
            "_egress_gateway_self_checks"
        ) == [gateway_check_path]
        proxy_response = _egress_raw_http(
            proxy_host,
            proxy_port,
            "GET http://egress.invalid/self-check HTTP/1.1\r\nHost: egress.invalid\r\nX-Egress-Self: proxy-self-check\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            ),
        )
        proxy_self_check = (
            proxy_response.startswith(b"HTTP/1.1 502 ")
            and len(proxy_cfg["_egress_proxy_connections"]) == 1
            and (proxy_cfg["_egress_proxy_connections"][0].get("kind") == "self-check")
        )
        proxy_url = f"http://{proxy_host}:{proxy_port}"
        inject = bool(case.get("egress_proxy_inject"))
        proxy_values = {
            key: proxy_url if inject else ""
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
        }
        env = {**dict(case.get("env") or {}), **proxy_values, "NO_PROXY": "", "no_proxy": ""}
        subcase = {**case, "env": env}
        base_url = f"http://{gateway_host}:{gateway_port}"
        rc, stderr, status, repo = _run_entry(agent_dir, base_url, subcase)
        gateway = _gateway_snapshot(cfg, status)
        product_rows = [row for row in proxy_cfg["_egress_proxy_connections"] if row.get("kind") == "product"]
        environment_exact = (
            all((env.get(key) == value for key, value in proxy_values.items()))
            and env.get("NO_PROXY") == ""
            and (env.get("no_proxy") == "")
        )
        status["_egress_proxy"] = {
            "gateway_self_check": gateway_self_check,
            "proxy_self_check": proxy_self_check,
            "proxy_injected": inject,
            "environment_exact": environment_exact,
            "gateway_host": gateway_host,
            "proxy_product_connections": len(product_rows),
            "proxy_api_key_seen": any((row.get("api_key_seen") is True for row in product_rows)),
            "proxy_methods": [row.get("method") for row in product_rows],
            "cleanup_complete": True,
        }
    finally:
        gateway_server.shutdown()
        proxy_server.shutdown()
        gateway_server.server_close()
        proxy_server.server_close()
        gateway_thread.join(timeout=1)
        proxy_thread.join(timeout=1)
    cleanup = not gateway_thread.is_alive() and (not proxy_thread.is_alive())
    gateway = _gateway_snapshot(cfg, status)
    gateway["cleanup_complete"] = cleanup
    status["_gateway"] = gateway
    if "_egress_proxy" in status:
        status["_egress_proxy"]["cleanup_complete"] = cleanup
    return (rc, stderr, status, repo, list(cfg.get("requests") or []))


def _gateway_cfg(case: dict) -> dict:
    cfg_kw = dict(
        mode=case["mode"],
        command=case.get("command", "echo MOCK_PROOF_7731 > mock_proof.txt"),
        usage_tokens=(12, 8),
        reject_substr="",
        strict=False,
        probe_token=case.get("probe_token", ""),
        reply_content=case.get("reply_content", ""),
        err_before=2,
        err_status=429,
        target_command="",
        target_left="",
        target_right="",
        structured_prompt_token="",
        structured_prompt_end="",
        structured_prompt_expected="",
        structured_prompt_fragment="",
    )
    if case.get("rounds") is not None:
        cfg_kw["rounds"] = case["rounds"]
    for key in (
        "probe_tokens",
        "allow_cmd",
        "deny_cmd",
        "probe_tool",
        "probe_args",
        "record_responses",
        "response_mutation",
        "retry_after",
        "block_response_s",
        "wire_tool_names",
        "wire_tool_capability",
        "peer_web_status",
        "peer_web_body",
        "peer_web_path",
    ):
        if key in case:
            cfg_kw[key] = case[key]
    cfg = MG.new_cfg(**cfg_kw)
    if cfg.get("wire_tool_names") or cfg.get("wire_tool_capability"):
        cfg["_peer_wire_lock"] = threading.Lock()
    if cfg.get("block_response_s"):
        cfg["_block_release_event"] = threading.Event()
        cfg["_block_handler_done"] = threading.Event()
    return cfg


def _gateway_snapshot(cfg: dict, status: dict) -> dict:
    request_times = list(cfg.get("_request_times_ns") or [])
    intervals = [
        round((request_times[index] - request_times[index - 1]) / 1000000000, 3)
        for index in range(1, len(request_times))
    ]
    responses = copy.deepcopy(list(cfg.get("_response_facts") or []))
    ended_ns = status.get("_process_ended_ns")
    block_entered = any((row.get("block_entered_ns") is not None for row in responses))
    block_pending = any(
        (
            row.get("block_entered_ns") is not None
            and (
                row.get("block_released_ns") is None
                or (type(ended_ns) is int and row["block_released_ns"] > ended_ns)
            )
            for row in responses
        )
    )
    return {
        "request_count": len(request_times),
        "request_intervals_s": intervals,
        "first_request_ns": request_times[0] if request_times else None,
        "last_request_ns": request_times[-1] if request_times else None,
        "response_statuses": [row["status"] for row in responses],
        "retry_after_headers": [row.get("retry_after") for row in responses],
        "response_usage_shapes": [row.get("usage_shape") or {} for row in responses],
        "response_usage_values": [row.get("usage_value") or {} for row in responses],
        "response_acceptance_shapes": [row.get("acceptance_shape") for row in responses],
        "response_send_complete": [row.get("send_complete") is True for row in responses],
        "block_entered": block_entered,
        "block_pending_at_process_exit": block_pending,
        "web_gets": copy.deepcopy(list(cfg.get("_peer_web_gets") or [])),
        "cleanup_complete": True,
    }


def _peer_replace_token(value, old: str, new: str):
    if isinstance(value, dict):
        return {key: _peer_replace_token(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_peer_replace_token(item, old, new) for item in value]
    if isinstance(value, tuple):
        return tuple((_peer_replace_token(item, old, new) for item in value))
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def _execute_mock_case(agent_dir: Path, case: dict):
    if case.get("egress_proxy"):
        return _execute_egress_proxy_case(agent_dir, case)
    cfg = _gateway_cfg(case)
    if cfg.get("record_responses"):
        srv = _MeterServer(("127.0.0.1", 0), _MeterHandler)
        srv.cfg = cfg
    else:
        srv = MG.make_server(cfg, 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    root_url = f"http://127.0.0.1:{port}"
    if cfg.get("peer_web_path"):
        web_url = root_url + str(cfg["peer_web_path"])
        cfg["rounds"] = _peer_replace_token(
            copy.deepcopy(cfg.get("rounds") or []), _PEER_WEB_URL_PLACEHOLDER, web_url
        )
    base_url = root_url + ""
    status = {}
    gateway = {}
    try:
        rc, stderr, status, repo = _run_entry(agent_dir, base_url, case)
        reqs = list(cfg["requests"])
        gateway = _gateway_snapshot(cfg, status) if cfg.get("record_responses") else {}
    finally:
        srv.shutdown()
        release = cfg.get("_block_release_event")
        if release is not None:
            release.set()
        done = cfg.get("_block_handler_done")
        handler_clean = done.wait(1.0) if done is not None else True
        srv.server_close()
        thread.join(timeout=1.0)
        if gateway:
            gateway["cleanup_complete"] = handler_clean and (not thread.is_alive())
            status["_gateway"] = gateway
    return (rc, stderr, status, repo, reqs)


def _variant_case(case: dict, variant: dict) -> dict:
    merged = dict(case)
    merged.pop("variants", None)
    merged.pop("check_variants", None)
    base_env = dict(case.get("env") or {})
    variant_env = dict(variant.get("env") or {})
    merged.update({key: value for key, value in variant.items() if key not in {"label", "env"}})
    merged["env"] = {**base_env, **variant_env}
    merged["id"] = f"{case['id']}__{variant['label']}"
    return merged


def _run_matrix_one(agent_dir: Path, case: dict) -> dict:
    observations = []
    stderr_parts = []
    total_elapsed = 0.0
    total_budget = 0.0
    try:
        for variant in case["variants"]:
            subcase = _variant_case(case, variant)
            rc, stderr, status, repo, reqs = _execute_mock_case(agent_dir, subcase)
            stderr_parts.append(stderr or "")
            total_elapsed += float(status.get("_elapsed_s") or 0.0)
            total_budget += float(status.get("_budget_s") or 0.0)
            observations.append(
                {"label": variant["label"], "rc": rc, "status": status, "repo": repo, "reqs": reqs}
            )
        ok, detail = case["check_variants"](observations)
    except Exception as exc:
        ok, detail = (False, f"runner error: {exc!r}")
    stderr = "\n".join(stderr_parts)
    if NO_TB in stderr and True:
        ok = False
        detail += " | STDERR-TRACEBACK"
    tb_tail = ""
    if not ok and NO_TB in stderr:
        tb_tail = stderr.strip().splitlines()[-1][:200]
    elapsed_s = round(total_elapsed, 1)
    budget_s = round(total_budget, 1)
    detail += f" | matrix_t={elapsed_s}s/{budget_s}s"
    return {
        "id": case["id"],
        "ok": bool(ok),
        "blocking": case.get("blocking", True),
        "why": detail,
        "tb_tail": tb_tail,
        "elapsed_s": elapsed_s,
        "budget_s": budget_s,
        "evidence": [],
    }


def _selected_cases(cases: list) -> list:
    """Run only the cases named in `D2A_ONLY_IDS` (comma separated); unset = full suite, bit-identical behaviour.

    Lets a single case be re-tested on its own without running the whole layer.
    WARNING: a filtered run is NOT a complete score. The `D2A_ONLY_ACTIVE` line is the explicit
    marker for the merge tool; without it results must not be merged as an increment.
    """
    only = {s.strip() for s in (os.environ.get("D2A_ONLY_IDS") or "").split(",") if s.strip()}
    if not only:
        return list(cases)
    picked = SC.dependency_closure(cases, only)
    need = {c["id"] for c in picked}
    print(
        f"D2A_ONLY_ACTIVE={len(picked)}/{len(cases)} ids={sorted(only)[:12]} gates+={sorted(need - only)[:8]}",
        flush=True,
    )
    return picked


def _run_one(agent_dir: Path, case: dict) -> dict:
    """Run one case on its own mock server (own cfg, own free port), isolated from the others, so cases can run concurrently."""
    if case.get("static_artifact"):
        ok, why = case["check"](Path(agent_dir))
        return {
            "id": case["id"],
            "ok": bool(ok),
            "blocking": case.get("blocking", True),
            "why": why,
            "tb_tail": "",
            "elapsed_s": None,
            "budget_s": None,
        }
    if case.get("variants") is not None:
        return _run_matrix_one(agent_dir, case)
    stderr = ""
    status = {}
    evidence = []
    try:
        rc, stderr, status, repo, reqs = _execute_mock_case(agent_dir, case)
        ok, detail = case["check"](rc, status, repo, reqs)
    except Exception as e:
        ok, detail = (False, f"runner error: {e!r}")
    if NO_TB in (stderr or "") and True:
        ok = False
        detail += " | STDERR-TRACEBACK"
    tb_tail = ""
    if not ok and (stderr or "").strip() and (NO_TB in stderr):
        tb_tail = stderr.strip().splitlines()[-1][:200]
    elapsed_s, budget_s = ((status or {}).get("_elapsed_s"), (status or {}).get("_budget_s"))
    if elapsed_s is not None:
        detail += f" | t={elapsed_s}s/{budget_s}s"
    return {
        "id": case["id"],
        "ok": bool(ok),
        "blocking": case.get("blocking", True),
        "why": detail,
        "tb_tail": tb_tail,
        "elapsed_s": elapsed_s,
        "budget_s": budget_s,
        "evidence": evidence,
    }


def run_smoke(agent_dir: Path, workers: int = 8) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    cases = _selected_cases(CASES)
    print(
        f"[cases] agent={agent_dir.parent.name}/{agent_dir.name}  workers={workers}  cases={len(cases)}\n",
        flush=True,
    )
    results: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_one, agent_dir, c): c["id"] for c in cases}
        for fut in futs:
            r = fut.result()
            results[r["id"]] = r
    return SC.apply_gates(cases, (results[c["id"]] for c in cases))


def _slim_result(row: dict) -> dict:
    return {
        "id": row["id"],
        "ok": row["ok"],
        "blocking": row["blocking"],
        "status": row["status"],
        "why": row["why"] + (f" | tb_tail={row['tb_tail']}" if row.get("tb_tail") else ""),
        "evidence": list(row.get("evidence") or []),
    }


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "agent_dir", help="artifact dir containing entry.py"
    )
    ap.add_argument("--workers", type=int, default=8, help="concurrent cases (default 8)")
    ap.add_argument(
        "--emit-json", action="store_true", help="also print MOCK_RESULTS=<json> for the pipeline to parse"
    )
    args = ap.parse_args()
    agent_dir = Path(args.agent_dir)
    if not (agent_dir / "entry.py").exists():
        sys.exit(f"entry.py not found: {agent_dir}")
    ordered = run_smoke(agent_dir, max(1, args.workers))
    npass = sum((r["ok"] for r in ordered))
    nblock = sum((r["blocking"] for r in ordered))
    nblock_pass = sum((r["ok"] for r in ordered if r["blocking"]))
    for r in ordered:
        tag = "" if r["blocking"] else " [info]"
        print(f"[{('PASS' if r['ok'] else 'FAIL')}]{tag} {r['id']:24s} {r['why']}", flush=True)
        if r["tb_tail"]:
            print(f"        tb_tail: {r['tb_tail']}", flush=True)
    if args.emit_json:
        slim = [_slim_result(r) for r in ordered]
        print("MOCK_RESULTS=" + json.dumps(slim, ensure_ascii=False), flush=True)
        print("MOCK_SMOKE_OK" if nblock_pass == nblock else "MOCK_SMOKE_FAIL", flush=True)
    print(f"\n== blocking {nblock_pass}/{nblock} passed | total {npass}/{len(ordered)} ==", flush=True)
    return 0 if nblock_pass == nblock else 1


if __name__ == "__main__":
    sys.exit(main())
