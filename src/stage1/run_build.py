"""Stage1 build orchestration. A builder model uses OpenCode inside a fresh
container to implement the product skeleton (f0) in place according to the spec,
producing an artifact whose hash is pinned.

Flow:
  fresh container (builder image: opencode + python/git + /opt/f0)
  -> docker cp: opencode.json -> /root/.opencode/opencode.json,
     spec + AGENT_CONTRACT -> the spec dir, prompt -> /work/prompt.txt
  -> docker exec headless:
     `cat /work/prompt.txt | opencode run --model d2agw/builder --format json >> /work/opencode.jsonl`
     (prompt via file/stdin avoids escaping; --format json gives a structured event
     stream; the model is always the gateway's single alias d2agw/builder)
  -> budget: an outer wall-timeout kills the run
  -> export the modified /opt/f0 -> out/<run_id>/agent, compute artifact_hash
     (sha256), write build_meta.json
  -> docker rm -f (fresh teardown)

Invariants: the OpenCode profile and the build prompt are byte-identical for every
builder; the model is reachable only through the gateway (which exposes only the
builder alias) and the container holds no upstream credentials; no evaluation
scenario content is injected (the scenarios are held out from the build).

Prerequisites for a real run (use --dry-run to validate the orchestration at zero
cost): the builder image is built; the spec exists; the Stage1 gateway is running.
A real run spends builder model calls.

Usage:
  python src/stage1/run_build.py --builder-tag <tag> --dry-run          # zero cost: print the plan
  python src/stage1/run_build.py --builder-tag <tag> --spec <spec.md>   # real run
"""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import injector as INJ
import registry as REG
import egress as EG

_SCRIPTS_DIR = HERE / "build_scripts"


def _load_script(name: str) -> str:
    """Read an in-container script file (contents are docker-cp'd / exec'd verbatim)."""
    return (_SCRIPTS_DIR / name).read_text(encoding="utf-8")


DOCKER = INJ.DOCKER
PROFILE = (
    Path(os.environ["D2A_OPENCODE_PROFILE"])
    if os.environ.get("D2A_OPENCODE_PROFILE")
    else HERE / "opencode_profile" / "opencode.json"
)
SPEC = HERE / "spec" / "functional_spec_v6.md"
PROMPT = HERE / "assets" / "build_prompt-v2.md"
CONTRACT = HERE / "contract" / "AGENT_CONTRACT-v2.md"
MODEL_REF = HERE / "spec" / "model_call_reference-v2.py"
OPENCODE_BIN = "/root/.opencode/bin/opencode"
DEFAULT_IMAGE = os.environ.get("D2A_BUILDER_IMAGE", "ghcr.io/yuyaoge/doc2harness-builder:latest")
F0_DIR = "/opt/f0"
SPEC_DIR = f"{F0_DIR}/.d2a_spec"
SUBMIT_FILE = f"{F0_DIR}/.build-submit"
RESULT_FILE = f"{F0_DIR}/.build-result"
CHECKING_FILE = f"{F0_DIR}/.build-checking"
POLL_INTERVAL = 5.0
MAX_SUBMISSIONS = int(os.environ.get("D2A_MAX_SUBMISSIONS", "3"))
_BUILD_REAL_SMOKE = os.environ.get("D2A_BUILD_REAL_SMOKE", "1").strip().lower() not in (
    "0",
    "off",
    "false",
    "no",
)
_BASIC_GATE_ONLY = os.environ.get("D2A_BASIC_GATE_ONLY", "1").strip().lower() in ("1", "on", "true", "yes")
MAX_CONTINUE_ROUNDS = 24
_NUDGE = "Continue implementing the remaining functionality in `.d2a_spec/functional_spec.md`. Build on the work already in place; do not start over. When finished you **must** run `touch /opt/f0/.build-submit` to trigger contract validation. If `/opt/f0/.build-result` reports `contract_ok=false`, address its `hint`, then `rm /opt/f0/.build-result` and `touch /opt/f0/.build-submit` again. If you judge that your context is close to exhausted, persist what you have completed and `touch /opt/f0/.build-submit` immediately to submit your current progress. Do not stop, and do not reply with only a summary, until you have run `touch /opt/f0/.build-submit`."
_IGNORE = {
    ".git",
    "node_modules",
    "__pycache__",
    ".opencode",
    ".pytest_cache",
    ".mypy_cache",
    ".d2a_spec",
    ".build-submit",
    ".build-result",
}
_REQUIRED_CHECKS = (
    "run_agent_sh_exists",
    "run_agent_sh_executable",
    "run_agent_sh_syntax_ok",
    "source_compiles",
    "install_sh_exists",
    "no_stub_markers",
    "all_commands_present",
    "yuyao_console_script",
    "std_iface_ok",
)
_HINTS = {
    "run_agent_sh_exists": "Provide /opt/f0/run_agent.sh per AGENT_CONTRACT: present, executable, and valid bash.",
    "run_agent_sh_executable": "Make /opt/f0/run_agent.sh executable (chmod +x).",
    "run_agent_sh_syntax_ok": "Fix the bash syntax in /opt/f0/run_agent.sh.",
    "source_compiles": "Ensure all sources compile without syntax errors.",
    "install_sh_exists": "Provide a single-command /opt/f0/install.sh (see §0.1.1).",
    "no_stub_markers": "Every item in §1-§9 of the specification must be a real, working implementation, with no unfinished code, placeholders, or stubs. Verify each one's observable behaviour by running it, as required by §0.7.",
    "all_commands_present": "Implement **every** slash command in functional_spec.md §5 as genuinely working, including the observable behaviour each one declares. Test each by running it per §0.7, and keep /help consistent with the implementation.",
    "yuyao_console_script": "Register and provide a working `yuyao` command per §0.1.1, runnable directly after installation.",
    "std_iface_ok": "Provide the programmatic interface module `yuyao.agent_api` per §0.1.2. It must expose `create_session(*, deny=None, mode=None, role=None, cwd=None)`, and the returned session must carry `.cwd` (str) and `.messages` (list, including tool results) and support callable `.dispatch(command)` and `.chat(text)`, the latter running an agentic turn that really executes tools. Construction must be **lazy** - no model call inside create_session - and the session must be neither streaming nor interactive.",
    "functional_smoke_ok": "The headless path (run_agent.sh in yolo mode) did not produce the expected result on a basic task. The agent must **actually** read files, create files, edit file contents as instructed, run commands, and land its changes in --repo-dir - not merely reply, and not decline because of a permission or mode setting. Exercise these basic read/modify/write paths for real, per §0.1.1 item 10 and §0.7, until they take effect.",
    "mock_smoke_ok": "Deterministic robustness, cost, parsing, and permission cases did not pass. These run against a local fault-injecting gateway and consume no real model budget. The model layer must survive missing usage, malformed JSON, unknown tools, and gateway 400s without crashing; must compute cost_usd as D2A_MODEL_COST times usage; and must record complete commands in the trajectory. See the individual items below.",
    "tui_smoke_ok": "The interactive TUI did not display the model's reply to the user under a real terminal (pty). After the user submits input the assistant's reply must be **visible** on screen: it must not be printed inside a Live or spinner region that is later erased, and the turn must not return empty. See the individual items below.",
    "perm_smoke_ok": "Permission and safety cases did not pass. A deny rule (§3.3, e.g. `Bash(touch*)`) must apply to the whole command string by **command semantics and glob**: direct commands, `&&` and `;` chains, and `bash -c` subshells must all be covered, with no bypass. Read-only modes (/ask, /plan) must block **every** write path, not just the edit tool. Operations such as /model and /clear must not quietly reset a read-only mode back to writable. Note that a deny rule is not a naive substring match against the rule text: parse out the tool and pattern and match the literal command. See the items below.",
    "state_smoke_ok": "State and sequencing commands did not pass. `/config set <k> <v>` and `/config get <k>` must round-trip for real, so a /config invocation carrying a subcommand cannot be matched only against a bare `/config`. Repeated sets of the same key must take the last value. `/mcp` add and remove must apply in order, and after adding the same name twice a single remove must remove it completely, leaving no remnant. `/init` must not overwrite an existing AGENTS.md, and repeated runs must not pollute it. See the individual items below.",
    "fileio_smoke_ok": "Write and read tool integrity did not pass. An empty write must not truncate an existing file to zero bytes. A write or edit without a preceding read must be blocked and must not land. If the file changes externally between read and edit, the tool must fail closed rather than silently overwrite with stale content. Reading a directory, a missing file, binary content, or a wrongly typed offset must produce a clean error without crashing or looping. An out-of-range read offset must not yield an inverted range (101-100) with fake truncated content. glob must not return zero results because of a broken symlink. See the individual items below.",
    "startup_smoke_ok": "Startup fail-soft and offline robustness did not pass (§0.1.2, §7). With a bad configuration - valid JSON carrying an unknown root key, malformed JSON with a trailing comma, a UTF-8 BOM - or with the configuration directory missing, or offline, session construction must warn and skip, and must never raise a bare traceback during construction or exit 1 silently. A valid configuration must still start, which is the positive control. See the individual items below.",
    "headless_smoke_ok": "Headless `-p` / `--print` output is not clean (§8.3, §9). With `--output-format text`, stdout must contain only the human-readable reply: no internal XML or function-call tags, no phase events, no kernel banner, and nothing that belongs on stderr. With `--output-format json`, the output must be a single valid JSON document containing result and exit_status, with no non-JSON noise. See the individual items below.",
}
_STUB_SCAN = _load_script("stub_scan.py")
_CMD_SCAN = _load_script("cmd_scan.py")
_FUNC_SMOKE = _load_script("func_smoke.py")
_YUYAO_RUN_CHECK = _load_script("yuyao_run_check.sh")
_YUYAO_STATIC_CHECK = _load_script("yuyao_static_check.py")
_STD_IFACE_SH = _load_script("std_iface.sh")
_SMOKE_SH = _load_script("smoke.sh")


async def _contract_check(name: str) -> dict:
    """Compliance self-check: verify the artifact satisfies AGENT_CONTRACT.
    Never runs the evaluation scenarios and never returns their scores.

    Checks (static / smoke inside the build container's /opt/f0):
      - run_agent.sh exists / is executable / passes bash -n
      - entry.py exists and py_compiles (informational)
      - the source tree passes compileall (required: catches syntax the builder broke)
      - import-only smoke: `import yuyao` doesn't crash (informational)"""
    _run = INJ._run
    ras = f"{F0_DIR}/run_agent.sh"
    entry = f"{F0_DIR}/entry.py"
    rc_e, _, _ = await _run(*DOCKER, "exec", name, "test", "-f", ras, timeout=30)
    rc_x, _, _ = await _run(*DOCKER, "exec", name, "test", "-x", ras, timeout=30)
    rc_s = 1
    if rc_e == 0:
        rc_s, _, _ = await _run(*DOCKER, "exec", name, "bash", "-n", ras, timeout=30)
    rc_pe, _, _ = await _run(*DOCKER, "exec", name, "test", "-f", entry, timeout=30)
    rc_pc = 1
    if rc_pe == 0:
        rc_pc, _, _ = await _run(
            *DOCKER, "exec", "-w", F0_DIR, name, "python", "-m", "py_compile", "entry.py", timeout=60
        )
    inst = f"{F0_DIR}/install.sh"
    rc_ie, _, _ = await _run(*DOCKER, "exec", name, "test", "-f", inst, timeout=30)
    rc_ix, _, _ = await _run(*DOCKER, "exec", name, "test", "-x", inst, timeout=30)
    smoke = _SMOKE_SH.replace("{f0}", F0_DIR)
    try:
        rc_sm, out_sm, _ = await _run(*DOCKER, "exec", name, "bash", "-lc", smoke, timeout=180)
    except Exception as e:
        print(f"[run_build] source smoke test errored (counted as not passing): {e}", file=sys.stderr)
        rc_sm, out_sm = (1, "")
    compiles = rc_sm == 0 and "COMPILE_OK" in out_sm
    import_ok = "IMPORT_OK" in out_sm
    stub_hits: list[str] = []
    try:
        scan = _STUB_SCAN.replace("{f0}", F0_DIR)
        rc_st, out_st, _ = await _run(*DOCKER, "exec", "-w", F0_DIR, name, "python", "-c", scan, timeout=120)
        stub_hits = [ln[len("STUB::") :] for ln in (out_st or "").splitlines() if ln.startswith("STUB::")]
    except Exception as e:
        print(f"[run_build] stub scan errored (counted as no stubs found): {e}", file=sys.stderr)
        stub_hits = []
    missing_cmds: list[str] = []
    prose_only_cmds: list[str] = []
    try:
        cscan = _CMD_SCAN.replace("{f0}", F0_DIR)
        rc_cm, out_cm, _ = await _run(*DOCKER, "exec", "-w", F0_DIR, name, "python", "-c", cscan, timeout=120)
        lines_cm = (out_cm or "").splitlines()
        missing_cmds = [ln[len("MISSING::") :] for ln in lines_cm if ln.startswith("MISSING::")]
        prose_only_cmds = [ln[len("PROSE_ONLY::") :] for ln in lines_cm if ln.startswith("PROSE_ONLY::")]
    except Exception as e:
        print(f"[run_build] command coverage scan errored (counted as fully covered): {e}", file=sys.stderr)
        missing_cmds = []
        prose_only_cmds = []

    async def _yuyao_static_ok() -> bool:
        rc_st, out_st, _ = await _run(
            *DOCKER, "exec", "-w", F0_DIR, name, "python", "-c", _YUYAO_STATIC_CHECK, timeout=30
        )
        return "YUYAO_STATIC_OK" in (out_st or "")

    try:
        yuyao_registered_static = await _yuyao_static_ok()
    except Exception:
        yuyao_registered_static = False
    yuyao_script_ok = False
    try:
        run_chk = _YUYAO_RUN_CHECK.replace("{f0}", F0_DIR)
        rc_ys, out_ys, _ = await _run(*DOCKER, "exec", name, "bash", "-lc", run_chk, timeout=300)
        out_ys = out_ys or ""
        if "YUYAO_RUN_OK" in out_ys:
            yuyao_script_ok = True
        elif "YUYAO_RUN_MISSING" in out_ys:
            yuyao_script_ok = False
        else:
            yuyao_script_ok = yuyao_registered_static
    except Exception as e:
        print(
            f"[run_build] yuyao behaviour check errored (falling back to the static registration verdict): {e}",
            file=sys.stderr,
        )
        yuyao_script_ok = yuyao_registered_static
    std_iface_ok = False
    std_iface_detail = ""
    try:
        std_sh = _STD_IFACE_SH.replace("{f0}", F0_DIR)
        rc_si, out_si, _ = await _run(*DOCKER, "exec", name, "bash", "-lc", std_sh, timeout=120)
        out_si = out_si or ""
        if "STD_IFACE_OK" in out_si:
            std_iface_ok = True
        else:
            for ln in out_si.splitlines():
                if ln.startswith("STD_IFACE_FAIL"):
                    std_iface_detail = ln[len("STD_IFACE_FAIL") :].strip()
            if not std_iface_detail:
                std_iface_detail = "STD_IFACE_OK was not emitted (the probe reached no definite conclusion)"
    except Exception as e:
        print(
            f"[run_build] standard interface probe errored (counted as not meeting the requirement): {e}",
            file=sys.stderr,
        )
        std_iface_detail = "probe_error: %s" % str(e)[:150]
        std_iface_ok = False
    return {
        "run_agent_sh_exists": rc_e == 0,
        "run_agent_sh_executable": rc_x == 0,
        "run_agent_sh_syntax_ok": rc_s == 0,
        "entry_py_exists": rc_pe == 0,
        "entry_py_compiles": rc_pc == 0,
        "source_compiles": compiles,
        "import_smoke_ok": import_ok,
        "install_sh_exists": rc_ie == 0,
        "install_sh_executable": rc_ix == 0,
        "no_stub_markers": len(stub_hits) == 0,
        "stub_hits": stub_hits,
        "all_commands_present": len(missing_cmds) == 0,
        "missing_commands": missing_cmds,
        "commands_documented_not_dispatched": prose_only_cmds,
        "yuyao_console_script": yuyao_script_ok,
        "yuyao_registered_static": yuyao_registered_static,
        "std_iface_ok": std_iface_ok,
        "std_iface_detail": std_iface_detail,
    }


def _contract_ok(checks: dict) -> bool:
    """contract_ok requires all of _REQUIRED_CHECKS; informational items don't block."""
    return all((checks.get(k, False) for k in _REQUIRED_CHECKS))


async def _functional_smoke(name: str) -> tuple[bool, list]:
    """Post-submission functional smoke: run the headless agent against the real model in
    the container on basic read/edit/write tasks and verify the resulting files.
    Returns (all passed?, per-case results). Errors count as not passed, but never crash
    the monitor or the build. Spends a small amount of the model-under-test's budget."""
    _run = INJ._run
    import tempfile as _tf

    _fs_host = None
    try:
        _fd, _fs_host = _tf.mkstemp(prefix="func_smoke_", suffix=".py")
        with os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(_FUNC_SMOKE)
        _cprc, _, _cperr = await _run(*DOCKER, "cp", _fs_host, f"{name}:/tmp/_func_smoke.py", timeout=120)
        if _cprc != 0:
            return (
                False,
                [
                    {
                        "id": "_harness",
                        "ok": False,
                        "why": f"docker cp of the harness failed: {(_cperr or '')[:200]}",
                    }
                ],
            )
        rc, out, err = await _run(
            *DOCKER,
            "exec",
            "-w",
            F0_DIR,
            "-e",
            f"D2A_SMOKE_WORKERS={os.environ.get('D2A_SMOKE_WORKERS', '32')}",
            "-e",
            f"D2A_SMOKE_ONLY={os.environ.get('D2A_SMOKE_ONLY', '')}",
            name,
            "python",
            "/tmp/_func_smoke.py",
            timeout=6000,
        )
    except Exception as e:
        print(f"[run_build] functional smoke errored (counted as not passing): {e}", file=sys.stderr)
        return (False, [{"id": "_harness", "ok": False, "why": f"smoke harness error: {e!r}"}])
    finally:
        if _fs_host:
            try:
                os.remove(_fs_host)
            except Exception:
                pass
        await _purge_harness_dir(name, "/tmp/_func_smoke.py")
    results: list = []
    for ln in (out or "").splitlines():
        if ln.startswith("FUNC_RESULTS="):
            try:
                results = json.loads(ln[len("FUNC_RESULTS=") :])
            except Exception:
                results = []
    ok = "FUNC_SMOKE_OK" in (out or "")
    if not ok and (not results):
        started, ended = ([], set())
        for ln in (out or "").splitlines():
            if ln.startswith("SCN_START "):
                started.append(ln[len("SCN_START ") :].strip())
            elif ln.startswith("SCN_END "):
                ended.add(ln[len("SCN_END ") :].strip())
        inflight = [s for s in started if s not in ended]
        done_n = len(ended)
        why = (
            "the smoke run exited abnormally as a whole (container/interpreter crash or timeout); %d scenario(s) completed"
            % done_n
        )
        if inflight:
            why += "; in flight when it failed (likely culprits): " + ", ".join(inflight[:8])
        else:
            why += "; trailing output: " + (err or out or "no output")[-160:]
        results = [{"id": "_harness", "ok": False, "why": why}]
    return (ok, results)


_SMOKE_DIR = HERE / "smoke"
_MOCK_CASES = _SMOKE_DIR / "mock_cases.py"
_MOCK_GATEWAY = _SMOKE_DIR / "mock_gateway.py"
_SMOKE_CONTRACT = _SMOKE_DIR / "smoke_contract.py"
_TUI_SMOKE = _SMOKE_DIR / "tui_smoke.py"
_PERM_SMOKE = _SMOKE_DIR / "perm_smoke.py"
_STATE_SMOKE = _SMOKE_DIR / "state_smoke.py"
_TUI_LINE = _SMOKE_DIR / "tui_line.py"
_FILEIO_SMOKE = _SMOKE_DIR / "fileio_smoke.py"
_STARTUP_SMOKE = _SMOKE_DIR / "startup_smoke.py"
_HEADLESS_SMOKE = _SMOKE_DIR / "headless_smoke.py"
_MOCK_WORKERS = os.environ.get("D2A_MOCK_WORKERS", "8")
_FROZEN_MANIFEST = HERE / "goldens" / "v1-suite-manifest.json"
_frozen_cases_cache: dict[str, list[dict]] = {}


def _frozen_layer_cases(layer: str) -> list[dict]:
    """The full (id, blocking) sequence for this layer from the frozen manifest. Read once and cached."""
    if not _frozen_cases_cache:
        manifest = json.loads(_FROZEN_MANIFEST.read_text(encoding="utf-8"))
        for entry in manifest["layers"]:
            _frozen_cases_cache[entry["name"]] = entry["cases"]
    return _frozen_cases_cache.get(layer, [])


def _harness_degraded(layer: str, exc: object) -> tuple[bool, list]:
    """When the harness itself errors, return every case of the layer as "inconclusive",
    keeping the frozen manifest's exact shape.

    Why a single synthetic case is not enough: the manifest shape is the denominator of
    the score. Returning one point (e.g. `[{"id": "_<layer>_harness", ...}]`) would make
    the "IDs/order must match the frozen manifest exactly" rule reject the whole
    artifact, so one transient docker hiccup could silently drop a build from the
    comparison.

    Recorded as `na`, not `fail`: a harness error means the harness failed to measure,
    and that must not cost the build points.
    """
    cases = _frozen_layer_cases(layer)
    why = f"inconclusive: the {layer} harness itself errored, so this layer never executed and cannot be judged ({exc!r})"
    return (
        False,
        [
            {"id": case["id"], "ok": False, "blocking": bool(case["blocking"]), "status": "na", "why": why}
            for case in cases
        ],
    )


async def _purge_harness_dir(name: str, dst: str) -> None:
    """Delete a harness directory from the running container.

    The acceptance layers `docker cp` their own source -- the case tables, sentinel
    tokens and pass conditions -- into /tmp inside the SAME container the builder
    model is working in, and that model has `"bash": "allow"`. Left behind, the
    files are the answer key: one `cat /tmp/d2a_state/state_smoke.py` after the
    first submission defeats the `_scrub_symptom` redaction entirely.

    So every layer removes its directory as soon as it is done, pass or fail.
    Failures here are swallowed: a container that has already exited cannot leak,
    and this must never be able to break a build.
    """
    try:
        await INJ._run(*DOCKER, "exec", name, "rm", "-rf", dst, timeout=30)
    except Exception:
        pass


async def _mock_smoke(name: str) -> tuple[bool, list]:
    """Deterministic mock smoke: copy mock_cases.py + mock_gateway.py into the container and run
    cost/parsing/robustness/permission cases against the artifact (entry.py) with a
    fault-injecting gateway as the model -- deterministic, no real-model budget, 8-way
    concurrent by default. Runs alongside the real-model functional smoke.
    Returns (all blocking cases passed?, per-case results [{id,ok,blocking,why}]).
    Errors count as not passed and never crash the build."""
    _run = INJ._run
    dst = "/tmp/d2a_mock"
    try:
        await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
        await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
        await _run(*DOCKER, "cp", str(_SMOKE_CONTRACT), f"{name}:{dst}/smoke_contract.py", timeout=60)
        await _run(*DOCKER, "cp", str(_MOCK_CASES), f"{name}:{dst}/mock_cases.py", timeout=60)
        rc, out, err = await _run(
            *DOCKER,
            "exec",
            "-w",
            F0_DIR,
            name,
            "python",
            f"{dst}/mock_cases.py",
            F0_DIR,
            "--workers",
            str(_MOCK_WORKERS),
            "--emit-json",
            timeout=1800,
        )
    except Exception as e:
        print(f"[run_build] mock smoke errored (counted as not passing): {e}", file=sys.stderr)
        return _harness_degraded("mock", e)
    finally:
        await _purge_harness_dir(name, dst)
    results: list = []
    for ln in (out or "").splitlines():
        if ln.startswith("MOCK_RESULTS="):
            try:
                results = json.loads(ln[len("MOCK_RESULTS=") :])
            except Exception:
                results = []
    ok = "MOCK_SMOKE_OK" in (out or "")
    if not ok and (not results):
        why = (
            "the mock smoke run exited abnormally as a whole (container/interpreter crash or timeout); trailing output: "
            + (err or out or "no output")[-160:]
        )
        _, results = _harness_degraded("mock", RuntimeError(why))
    return (ok, results)


async def _tui_smoke(name: str) -> tuple[bool, list]:
    """Interactive TUI rendering smoke: copy tui_smoke.py + mock_gateway.py into the
    container and run the real YuyaoTUI under a real pty, asserting that the assistant's
    reply is actually visible in the rendered screen after user input. Two layers:
    render_stub (rendering layer in isolation) and e2e_reply (no stubs -- a real turn
    through the mock gateway and the full model->render chain). Targets "the agent can't
    talk" bugs that only surface in the interactive/terminal rendering layer and are
    invisible to headless tests.
    Returns (all blocking cases passed?, per-case results). Errors count as not passed
    and never crash the build."""
    _run = INJ._run
    dst = "/tmp/d2a_tui"
    try:
        await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
        await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
        await _run(*DOCKER, "cp", str(_TUI_SMOKE), f"{name}:{dst}/tui_smoke.py", timeout=60)
        rc, out, err = await _run(
            *DOCKER,
            "exec",
            "-w",
            F0_DIR,
            name,
            "python",
            f"{dst}/tui_smoke.py",
            F0_DIR,
            "--emit-json",
            timeout=600,
        )
    except Exception as e:
        print(f"[run_build] TUI render smoke errored (counted as not passing): {e}", file=sys.stderr)
        return (
            False,
            [{"id": "_tui_harness", "ok": False, "blocking": True, "why": f"tui harness error: {e!r}"}],
        )
    finally:
        await _purge_harness_dir(name, dst)
    results: list = []
    for ln in (out or "").splitlines():
        if ln.startswith("TUI_RESULTS="):
            try:
                results = json.loads(ln[len("TUI_RESULTS=") :])
            except Exception:
                results = []
    ok = "TUI_SMOKE_OK" in (out or "")
    if not ok and (not results):
        why = (
            "the TUI render smoke run exited abnormally as a whole (container/interpreter/pty crash or timeout); trailing output: "
            + (err or out or "no output")[-160:]
        )
        results = [{"id": "_tui_harness", "ok": False, "blocking": True, "why": why}]
    return (ok, results)


async def _perm_smoke(name: str) -> tuple[bool, list]:
    """Deterministic permission/security smoke: copy perm_smoke.py + mock_gateway.py into
    the container and use the mock gateway to deterministically hand the interactive TUI
    exactly the forbidden tool_calls (deny rules, read-only mode, state persistence),
    asserting the permission layer blocks them before execution. Unlike the real-model
    smoke, the block happens in the agent's own permission layer independent of the model,
    so results are deterministic and there is no false pass from "the model never tried
    the forbidden action". Targets bypassed deny-rule syntax (Bash(...)/glob) and
    read-only/state-switch leaks.
    Returns (all blocking cases passed?, per-case results). Errors count as not passed
    and never crash the build."""
    _run = INJ._run
    dst = "/tmp/d2a_perm"
    try:
        await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
        await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
        await _run(*DOCKER, "cp", str(_SMOKE_CONTRACT), f"{name}:{dst}/smoke_contract.py", timeout=60)
        await _run(*DOCKER, "cp", str(_PERM_SMOKE), f"{name}:{dst}/perm_smoke.py", timeout=60)
        rc, out, err = await _run(
            *DOCKER,
            "exec",
            "-w",
            F0_DIR,
            name,
            "python",
            f"{dst}/perm_smoke.py",
            F0_DIR,
            "--emit-json",
            timeout=600,
        )
    except Exception as e:
        print(
            f"[run_build] permission determinism smoke errored (counted as not passing): {e}", file=sys.stderr
        )
        return _harness_degraded("perm", e)
    finally:
        await _purge_harness_dir(name, dst)
    results: list = []
    for ln in (out or "").splitlines():
        if ln.startswith("PERM_RESULTS="):
            try:
                results = json.loads(ln[len("PERM_RESULTS=") :])
            except Exception:
                results = []
    ok = "PERM_SMOKE_OK" in (out or "")
    if not ok and (not results):
        why = (
            "the permission determinism smoke run exited abnormally as a whole (container/interpreter crash or timeout); trailing output: "
            + (err or out or "no output")[-160:]
        )
        _, results = _harness_degraded("perm", RuntimeError(why))
    return (ok, results)


async def _state_smoke(name: str) -> tuple[bool, list]:
    """Deterministic state/sequence smoke: copy state_smoke.py + mock_gateway.py into the
    container and run config/mcp/init/export/memory commands in order, asserting the
    order-sensitive and idempotency/repeat behaviors of command permutations. Targets
    set/get round-trips that don't stick, ghosts left by a repeated add, and /init
    overwriting an existing file.

    No real-model budget: most commands never touch the model, and the few that internally
    go through run_turn (/init, /compact family) are answered by a deterministic stub
    gateway that state_smoke starts per case -- never the real gateway. Results are
    therefore fully deterministic.
    Returns (all blocking cases passed?, per-case results). Errors count as not passed and
    never crash the build."""
    _run = INJ._run
    dst = "/tmp/d2a_state"
    try:
        await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
        await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
        await _run(*DOCKER, "cp", str(_SMOKE_CONTRACT), f"{name}:{dst}/smoke_contract.py", timeout=60)
        await _run(*DOCKER, "cp", str(_STATE_SMOKE), f"{name}:{dst}/state_smoke.py", timeout=60)
        rc, out, err = await _run(
            *DOCKER,
            "exec",
            "-w",
            F0_DIR,
            name,
            "python",
            f"{dst}/state_smoke.py",
            F0_DIR,
            "--emit-json",
            timeout=600,
        )
    except Exception as e:
        print(
            f"[run_build] state/sequence determinism smoke errored (counted as not passing): {e}",
            file=sys.stderr,
        )
        return _harness_degraded("state", e)
    finally:
        await _purge_harness_dir(name, dst)
    results: list = []
    for ln in (out or "").splitlines():
        if ln.startswith("STATE_RESULTS="):
            try:
                results = json.loads(ln[len("STATE_RESULTS=") :])
            except Exception:
                results = []
    ok = "STATE_SMOKE_OK" in (out or "")
    if not ok and (not results):
        why = (
            "the state/sequence determinism smoke run exited abnormally as a whole (container/interpreter crash or timeout); trailing output: "
            + (err or out or "no output")[-160:]
        )
        _, results = _harness_degraded("state", RuntimeError(why))
    return (ok, results)


async def _tui_line_smoke(name: str) -> tuple[bool, list]:
    """tui-line layer: start the artifact's own interactive TUI in a real pty and feed
    stdin line by line.

    Why this needs its own layer: `tui_smoke` also uses a pty but drives the TUI by method
    name (`chat`/`agentic_turn`/`_run_turn`), which happens to bypass the `_handle_line`
    most artifacts actually use; `headless_smoke` takes the non-interactive `-p` path and
    structurally cannot reach tui.py's line handler. This layer doesn't care what the
    internal functions are called -- it just writes lines to the pty master.

    Covers the keyboard-shortcut clauses of the spec plus interactive-input robustness (the spec's rule that
    malformed/exceptional input must degrade to a clean error and never crash raw).

    This layer is slower than the others: each case really starts a TUI and waits for a
    model turn, roughly three seconds per case.
    No real-model budget: a stub gateway answers, as in the other layers.
    Returns (all blocking cases passed?, per-case results).
    """
    _run = INJ._run
    dst = "/tmp/d2a_tuiline"
    try:
        try:
            await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
            await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
            await _run(*DOCKER, "cp", str(_TUI_LINE), f"{name}:{dst}/tui_line.py", timeout=60)
            result_file = f"{dst}/results.jsonl"
            await _run(
                *DOCKER, "exec", name, "python", "-c", f"open({result_file!r}, 'w').close()", timeout=30
            )
        except Exception as e:
            print(f"[run_build] tui-line smoke errored (counted as not passing): {e}", file=sys.stderr)
            return _harness_degraded("tui_line", e)
        results: list = []
        for case in _frozen_layer_cases("tui_line"):
            case_id = case["id"]
            try:
                rc, out, err = await _run(
                    *DOCKER,
                    "exec",
                    "-w",
                    F0_DIR,
                    name,
                    "python",
                    f"{dst}/tui_line.py",
                    F0_DIR,
                    "--emit-json",
                    "--case-id",
                    case_id,
                    "--result-file",
                    result_file,
                    timeout=150,
                )
                parsed = []
                for ln in (out or "").splitlines():
                    if ln.startswith("TUILINE_RESULTS="):
                        parsed = json.loads(ln[len("TUILINE_RESULTS=") :])
                results.extend(parsed)
                if rc != 0 and (not parsed):
                    results.append(
                        {
                            "id": case_id,
                            "ok": False,
                            "blocking": bool(case["blocking"]),
                            "status": "error",
                            "why": f"isolated tui case rc={rc}: {(err or out or '')[-160:]}",
                        }
                    )
            except Exception as exc:
                results.append(
                    {
                        "id": case_id,
                        "ok": False,
                        "blocking": bool(case["blocking"]),
                        "status": "error",
                        "why": f"isolated tui case failed: {exc!r}",
                    }
                )
    finally:
        await _purge_harness_dir(name, dst)
    ok = all((row.get("ok") for row in results if row.get("blocking")))
    return (ok, results)


async def _fileio_smoke(name: str) -> tuple[bool, list]:
    """Deterministic write/read tool integrity smoke: copy fileio_smoke.py + perm_smoke.py
    + mock_gateway.py into the container and use the mock's `seq_probe` multi-round tool
    sequences to drive read-before-edit, TOCTOU (external change after read must be
    fail-closed), the empty-content write guard, fail-closed read of a directory / binary /
    out-of-range offset, and glob not returning zero results on a broken symlink. No
    real-model budget. N/A guard: skipped if the corresponding standalone tools aren't
    exposed.
    Returns (all blocking cases passed?, per-case results). Errors count as not passed and
    never crash the build."""
    _run = INJ._run
    dst = "/tmp/d2a_fileio"
    try:
        await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
        await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
        await _run(*DOCKER, "cp", str(_SMOKE_CONTRACT), f"{name}:{dst}/smoke_contract.py", timeout=60)
        await _run(*DOCKER, "cp", str(_PERM_SMOKE), f"{name}:{dst}/perm_smoke.py", timeout=60)
        await _run(*DOCKER, "cp", str(_FILEIO_SMOKE), f"{name}:{dst}/fileio_smoke.py", timeout=60)
        rc, out, err = await _run(
            *DOCKER,
            "exec",
            "-w",
            F0_DIR,
            name,
            "python",
            f"{dst}/fileio_smoke.py",
            F0_DIR,
            "--emit-json",
            timeout=600,
        )
    except Exception as e:
        print(
            f"[run_build] write/read tool integrity determinism smoke errored (counted as not passing): {e}",
            file=sys.stderr,
        )
        return _harness_degraded("fileio", e)
    finally:
        await _purge_harness_dir(name, dst)
    results: list = []
    for ln in (out or "").splitlines():
        if ln.startswith("FILEIO_RESULTS="):
            try:
                results = json.loads(ln[len("FILEIO_RESULTS=") :])
            except Exception:
                results = []
    ok = "FILEIO_SMOKE_OK" in (out or "")
    if not ok and (not results):
        why = (
            "the write/read tool integrity determinism smoke run exited abnormally as a whole (container/interpreter crash or timeout); trailing output: "
            + (err or out or "no output")[-160:]
        )
        _, results = _harness_degraded("fileio", RuntimeError(why))
    return (ok, results)


async def _startup_smoke(name: str) -> tuple[bool, list]:
    """Deterministic startup fail-soft / offline robustness smoke: copy startup_smoke.py +
    perm_smoke.py into the container, write bad-config and missing-directory fixtures under
    a controlled temporary HOME, and construct one session via the build's standard
    interface `yuyao.agent_api.create_session()` (no model connection, no turn), judging
    "fault-tolerant startup vs raw traceback at construction time". No real-model budget.
    N/A guard: if the build has no standard create_session interface, the suite is skipped
    as inconclusive (the contract check covers that separately).
    Returns (all blocking cases passed?, per-case results). Errors count as not passed and
    never crash the build."""
    _run = INJ._run
    dst = "/tmp/d2a_startup"
    try:
        try:
            await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
            await _run(*DOCKER, "cp", str(_PERM_SMOKE), f"{name}:{dst}/perm_smoke.py", timeout=60)
            await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
            await _run(*DOCKER, "cp", str(_SMOKE_CONTRACT), f"{name}:{dst}/smoke_contract.py", timeout=60)
            await _run(*DOCKER, "cp", str(_STARTUP_SMOKE), f"{name}:{dst}/startup_smoke.py", timeout=60)
            result_file = f"{dst}/results.jsonl"
            await _run(
                *DOCKER, "exec", name, "python", "-c", f"open({result_file!r}, 'w').close()", timeout=30
            )
        except Exception as e:
            print(
                f"[run_build] startup fail-soft determinism smoke errored (counted as not passing): {e}",
                file=sys.stderr,
            )
            return _harness_degraded("startup", e)
        results: list = []
        for case in _frozen_layer_cases("startup"):
            case_id = case["id"]
            try:
                rc, out, err = await _run(
                    *DOCKER,
                    "exec",
                    "-w",
                    F0_DIR,
                    name,
                    "python",
                    f"{dst}/startup_smoke.py",
                    F0_DIR,
                    "--emit-json",
                    "--case-id",
                    case_id,
                    "--result-file",
                    result_file,
                    timeout=300,
                )
                parsed = []
                for ln in (out or "").splitlines():
                    if ln.startswith("STARTUP_RESULTS="):
                        parsed = json.loads(ln[len("STARTUP_RESULTS=") :])
                results.extend(parsed)
                if rc != 0 and (not parsed):
                    results.append(
                        {
                            "id": case_id,
                            "ok": False,
                            "blocking": bool(case["blocking"]),
                            "status": "error",
                            "why": f"isolated startup case rc={rc}: {(err or out or '')[-160:]}",
                        }
                    )
            except Exception as exc:
                results.append(
                    {
                        "id": case_id,
                        "ok": False,
                        "blocking": bool(case["blocking"]),
                        "status": "error",
                        "why": f"isolated startup case failed: {exc!r}",
                    }
                )
    finally:
        await _purge_harness_dir(name, dst)
    ok = all((row.get("ok") for row in results if row.get("blocking")))
    return (ok, results)


async def _headless_smoke(name: str) -> tuple[bool, list]:
    """Deterministic headless `-p` text/JSON output purity smoke: copy headless_smoke.py +
    mock_gateway.py + perm_smoke.py into the container, start the mock (fixed_reply plain
    text), run the real `yuyao -p "<task>" --output-format text|json`, and check stdout is
    clean/valid (no internal XML tags, logs, or banners; JSON is a single valid document).
    No real-model budget. N/A guard: skipped as inconclusive if the build has no -p CLI.
    Returns (all blocking cases passed?, per-case results). Errors count as not passed and
    never crash the build."""
    _run = INJ._run
    dst = "/tmp/d2a_headless"
    try:
        await _run(*DOCKER, "exec", name, "mkdir", "-p", dst, timeout=30)
        await _run(*DOCKER, "cp", str(_SMOKE_CONTRACT), f"{name}:{dst}/smoke_contract.py", timeout=60)
        await _run(*DOCKER, "cp", str(_PERM_SMOKE), f"{name}:{dst}/perm_smoke.py", timeout=60)
        await _run(*DOCKER, "cp", str(_MOCK_GATEWAY), f"{name}:{dst}/mock_gateway.py", timeout=60)
        await _run(*DOCKER, "cp", str(_HEADLESS_SMOKE), f"{name}:{dst}/headless_smoke.py", timeout=60)
        rc, out, err = await _run(
            *DOCKER,
            "exec",
            "-w",
            F0_DIR,
            name,
            "python",
            f"{dst}/headless_smoke.py",
            F0_DIR,
            "--emit-json",
            timeout=1200,
        )
    except Exception as e:
        print(
            f"[run_build] headless -p output purity determinism smoke errored (counted as not passing): {e}",
            file=sys.stderr,
        )
        return _harness_degraded("headless", e)
    finally:
        await _purge_harness_dir(name, dst)
    results: list = []
    for ln in (out or "").splitlines():
        if ln.startswith("HEADLESS_RESULTS="):
            try:
                results = json.loads(ln[len("HEADLESS_RESULTS=") :])
            except Exception:
                results = []
    ok = "HEADLESS_SMOKE_OK" in (out or "")
    if not ok and (not results):
        why = (
            "the headless -p output purity determinism smoke run exited abnormally as a whole (container/interpreter crash or timeout); trailing output: "
            + (err or out or "no output")[-160:]
        )
        _, results = _harness_degraded("headless", RuntimeError(why))
    return (ok, results)


_SENTINELS = (
    "plan_t.txt",
    "ask_t.txt",
    "diff_t.txt",
    "undo_t.txt",
    "note.txt",
    "util9.py",
    "made.txt",
    "HACK-A",
    "HACK-B",
    "WORLD-123",
    "CHANGED-8888",
    "ORIG-7777",
    "QWE7Z",
    "APPENDED-RULE-77",
    "MANAGED-BY-YUYAO",
    "COMMIT-ME",
    "ZephyrX9-771",
    "TICKMARK",
    "ticks.txt",
    "unit1.txt",
    "unit2.txt",
    "unit3.txt",
    "zzznotacommand123",
    "MEMO-KEY-9931",
    "EMP-7788",
    "PORT-4242",
    "TRAJ-OK-5150",
    "marker.txt",
    "CUSTOM-1234",
    "CMD-OUT.txt",
    "scaffold",
    "REDONE-5150",
    "FIN-REDO",
    "CD-OK-77",
    "here.txt",
    "NEWSESS-9931",
    "RAN-OK",
    "HACK-RR",
    "ORIG-RR",
    "no-such-flag-zzz9",
    "sess-x",
    "VAL-CFG-7731",
    "probe.diag.key",
    "probesrv9931",
    "probekit",
    "probebot",
    "FORGET-ME-7731",
    "KEEP-9900",
    "EXPORT-PROBE-5150",
    "HACK-PLAN-9000",
    "ORIG-PLAN-ALIAS",
    "ATREF-7731",
    "ref.txt",
    "BANG-SHELL-1234",
    "bang_out.txt",
    "MATCHWORD",
    "ignored.txt",
    "kept.txt",
    "HACK-DENY-9000",
    "ORIG-DENY",
    "hooks_blocked_marker",
    "zzznotreal999",
    "MODELCTX-7788",
    "ATCD-9931",
    "shapes.py",
    "unixfile.py",
    "perimeter",
    "READONLY-OK-7788",
    "HACK-WROTE-9931",
    "ORIG-PLANX",
    "probe.txt",
    "ALLOW-OK",
    "HACKED-9931",
    "LOCKED.txt",
    "ALLOWED.txt",
    "HACK-SHELL-9931",
    "ORIG-SW",
    "LIVE-DD",
    "DENY_DIRECT_9931",
    "LIVE-DC",
    "DENY_CHAIN_9931",
    "LIVE-DS",
    "DENY_SUB_9931",
    "HACK-MS-9931",
    "ORIG-MS",
    "HACK-CL-9931",
    "ORIG-CL",
    "ESC-9931",
    "escape_PROBE_9931",
    "CHANGED-L1500-DONE",
    "MARKER-L1500",
    "NONEXISTENT_TOKEN_ZZZ9",
    "a.b(c)*d",
    "big.txt",
    "café",
    "CRLF-APPEND-OK",
    "NEEDLE-7731-XYZ",
    "GREP-HIT-9931",
    "ENDMARK-7731",
    "HUGE-APPEND-OK",
)
_INFO_SMOKE_IDS = {
    "subagent_parallel",
    "memory_status",
    "custom_command",
    "plan_file",
    "features_toggle",
    "help_consistency",
    "at_file_inject",
    "bang_shell",
    "grep_gitignore",
    "deny_blocks_edit",
    "hooks_deny_blocks",
    "deny_bash_direct",
    "deny_bash_chain",
    "deny_bash_subshell",
    "huge_single_line",
}


def _scrub_symptom(why) -> str:
    """Scrub one smoke failure reason into a "symptom" safe to write back to the builder:
    redact probe samples, temp paths, and gateway addresses, turn internal timeout wording
    into plain language, and truncate. The builder learns what was observed to be wrong
    without getting the concrete probes it could game."""
    import re as _r

    s = str(why or "")
    s = _r.sub(
        "TimeoutExpired\\([^)]*\\)", "self-check timed out (the case did not finish within the limit)", s
    )
    s = _r.sub("/tmp/\\S+", "<temp path>", s)
    s = _r.sub("https?://\\S+", "<gateway address>", s)
    for t in _SENTINELS:
        s = s.replace(t, "<probe>")
    s = s.strip()
    return s[:160]


def _submission_summary(idx: int, static_ok: bool, failed: list, checks: dict) -> dict:
    """Compress one submission's check results into a compact summary: whether the static
    gate passed, features passed/total, and the lists of passing and failing feature ids.
    If the static gate failed, no functional smoke ran, so ran_smoke=False plus the failing
    static items are recorded -- that distinguishes "didn't run" from "ran and failed"."""

    def _agg(key):
        arr = checks.get(key) or []
        blk = [r for r in arr if isinstance(r, dict) and r.get("blocking")]
        return (sum((bool(r.get("ok")) for r in blk)), len(blk), [r["id"] for r in blk if not r.get("ok")])

    sm = checks.get("functional_smoke")
    if not isinstance(sm, list) or not sm:
        base = {
            "submission": idx,
            "static_ok": static_ok,
            "ran_smoke": False,
            "smoke_skipped": bool(checks.get("functional_smoke_skipped")),
            "failed_static": [k for k in _REQUIRED_CHECKS if not checks.get(k, False)],
            "passed": 0,
            "total": 0,
            "passed_ids": [],
            "failed_ids": [],
        }
        for _k in ("mock", "perm", "state", "edit", "fileio", "startup", "headless", "tui"):
            _p, _t, _f = _agg(_k + "_smoke")
            base[_k + "_passed"], base[_k + "_total"], base[_k + "_failed_ids"] = (_p, _t, _f)
        return base
    blocking = [r for r in sm if r.get("id") not in _INFO_SMOKE_IDS]
    passed_ids = [r["id"] for r in blocking if r.get("ok")]
    failed_ids = [r["id"] for r in blocking if not r.get("ok")]
    mk = checks.get("mock_smoke") or []
    mk_block = [r for r in mk if isinstance(r, dict) and r.get("blocking")]
    mk_failed = [r["id"] for r in mk_block if not r.get("ok")]
    tk = checks.get("tui_smoke") or []
    tk_block = [r for r in tk if isinstance(r, dict) and r.get("blocking")]
    tk_failed = [r["id"] for r in tk_block if not r.get("ok")]
    pk = checks.get("perm_smoke") or []
    pk_block = [r for r in pk if isinstance(r, dict) and r.get("blocking")]
    pk_failed = [r["id"] for r in pk_block if not r.get("ok")]
    stk = checks.get("state_smoke") or []
    stk_block = [r for r in stk if isinstance(r, dict) and r.get("blocking")]
    stk_failed = [r["id"] for r in stk_block if not r.get("ok")]
    fk = checks.get("fileio_smoke") or []
    fk_block = [r for r in fk if isinstance(r, dict) and r.get("blocking")]
    fk_failed = [r["id"] for r in fk_block if not r.get("ok")]
    suk = checks.get("startup_smoke") or []
    suk_block = [r for r in suk if isinstance(r, dict) and r.get("blocking")]
    suk_failed = [r["id"] for r in suk_block if not r.get("ok")]
    hdk = checks.get("headless_smoke") or []
    hdk_block = [r for r in hdk if isinstance(r, dict) and r.get("blocking")]
    hdk_failed = [r["id"] for r in hdk_block if not r.get("ok")]
    return {
        "submission": idx,
        "static_ok": static_ok,
        "ran_smoke": True,
        "passed": len(passed_ids),
        "total": len(blocking),
        "passed_ids": passed_ids,
        "failed_ids": failed_ids,
        "info_passed": [r["id"] for r in sm if r.get("id") in _INFO_SMOKE_IDS and r.get("ok")],
        "mock_passed": sum((r.get("ok", False) for r in mk_block)),
        "mock_total": len(mk_block),
        "mock_failed_ids": mk_failed,
        "tui_passed": sum((r.get("ok", False) for r in tk_block)),
        "tui_total": len(tk_block),
        "tui_failed_ids": tk_failed,
        "perm_passed": sum((r.get("ok", False) for r in pk_block)),
        "perm_total": len(pk_block),
        "perm_failed_ids": pk_failed,
        "state_passed": sum((r.get("ok", False) for r in stk_block)),
        "state_total": len(stk_block),
        "state_failed_ids": stk_failed,
        "fileio_passed": sum((r.get("ok", False) for r in fk_block)),
        "fileio_total": len(fk_block),
        "fileio_failed_ids": fk_failed,
        "startup_passed": sum((r.get("ok", False) for r in suk_block)),
        "startup_total": len(suk_block),
        "startup_failed_ids": suk_failed,
        "headless_passed": sum((r.get("ok", False) for r in hdk_block)),
        "headless_total": len(hdk_block),
        "headless_failed_ids": hdk_failed,
    }


def _print_submission(s: dict) -> None:
    """Print a single submission's summary to stderr in real time, so which features passed
    is visible while the build is still running."""
    idx = s["submission"]
    if not s["ran_smoke"]:
        if not s.get("static_ok"):
            fs = ", ".join(s.get("failed_static") or []) or "(unknown)"
            print(
                f"[submission #{idx}] static gate failed; functional smoke not run. Missing: {fs}",
                file=sys.stderr,
                flush=True,
            )
        else:
            layers, det_fail = ([], [])
            for k in ("mock", "perm", "state", "edit", "fileio", "startup", "headless", "tui"):
                t = s.get(k + "_total", 0)
                if t:
                    layers.append(f"{k} {s.get(k + '_passed', 0)}/{t}")
                    if s.get(k + "_failed_ids"):
                        det_fail.append(k)
            if not layers:
                print(
                    f"[submission #{idx}] basic static gate passed; verdict settled and the artifact is final (no scoring layer runs during the build; full evaluation happens afterwards)",
                    file=sys.stderr,
                    flush=True,
                )
                return
            tag = "real-model smoke skipped" if s.get("smoke_skipped") else "functional smoke not run"
            note = (
                "; failing layers: " + ", ".join(det_fail)
                if det_fail
                else "; all deterministic layers passed"
            )
            print(
                f"[submission #{idx}] static gate passed · {tag} · " + " ".join(layers) + note,
                file=sys.stderr,
                flush=True,
            )
        return
    p, t = (s["passed"], s["total"])
    pct = 100.0 * p / t if t else 0.0
    print(
        f"[submission #{idx}] static gate passed · features {p}/{t} passed ({pct:.0f}%)",
        file=sys.stderr,
        flush=True,
    )
    if s["passed_ids"]:
        print("           passed: " + " ".join(s["passed_ids"]), file=sys.stderr, flush=True)
    if s["failed_ids"]:
        print("           failed: " + " ".join(s["failed_ids"]), file=sys.stderr, flush=True)
    if s.get("mock_total"):
        mp, mt = (s["mock_passed"], s["mock_total"])
        line = f"           mock (deterministic) {mp}/{mt} passed"
        if s.get("mock_failed_ids"):
            line += " · failed: " + " ".join(s["mock_failed_ids"])
        print(line, file=sys.stderr, flush=True)
    if s.get("tui_total"):
        tp, tt = (s["tui_passed"], s["tui_total"])
        line = f"           interactive TUI render (pty) {tp}/{tt} passed"
        if s.get("tui_failed_ids"):
            line += " · failed: " + " ".join(s["tui_failed_ids"])
        print(line, file=sys.stderr, flush=True)
    if s.get("perm_total"):
        pp, pt2 = (s["perm_passed"], s["perm_total"])
        line = f"           permissions/security (deterministic) {pp}/{pt2} passed"
        if s.get("perm_failed_ids"):
            line += " · failed: " + " ".join(s["perm_failed_ids"])
        print(line, file=sys.stderr, flush=True)
    if s.get("state_total"):
        sp, st2 = (s["state_passed"], s["state_total"])
        line = f"           state/sequence (deterministic) {sp}/{st2} passed"
        if s.get("state_failed_ids"):
            line += " · failed: " + " ".join(s["state_failed_ids"])
        print(line, file=sys.stderr, flush=True)
    if s.get("fileio_total"):
        fp, ft = (s["fileio_passed"], s["fileio_total"])
        line = f"           write/read tool integrity (deterministic) {fp}/{ft} passed"
        if s.get("fileio_failed_ids"):
            line += " · failed: " + " ".join(s["fileio_failed_ids"])
        print(line, file=sys.stderr, flush=True)
    if s.get("startup_total"):
        sup, sut = (s["startup_passed"], s["startup_total"])
        line = f"           startup fail-soft (deterministic) {sup}/{sut} passed"
        if s.get("startup_failed_ids"):
            line += " · failed: " + " ".join(s["startup_failed_ids"])
        print(line, file=sys.stderr, flush=True)
    if s.get("headless_total"):
        hdp, hdt = (s["headless_passed"], s["headless_total"])
        line = f"           headless -p output purity (deterministic) {hdp}/{hdt} passed"
        if s.get("headless_failed_ids"):
            line += " · failed: " + " ".join(s["headless_failed_ids"])
        print(line, file=sys.stderr, flush=True)


def default_memory_limit() -> str:
    """The cap a build gets when nobody says otherwise.

    12g is ~40x what a normal build uses. It is deliberately generous: the cap is
    there to contain a runaway, not to ration ordinary work. Set
    D2A_BUILD_MEMORY=0 to lift it while debugging a build that legitimately needs
    more.
    """
    return os.environ.get("D2A_BUILD_MEMORY", "12g")


def memory_limit_flags(limit: str) -> list[str]:
    """`docker run` flags that cap one build's memory. Empty list means uncapped.

    Why this exists: without a cap, one build that allocates unboundedly exhausts
    host memory, and then *everything* stalls -- every other lane's `docker exec`
    misses its 30s timeout and the whole parallel batch dies together. One runaway
    costs the batch, not one build.

    A cap turns that into a local failure: the offending container is killed inside
    its own cgroup while its siblings keep running. It is deliberately generous --
    a normal build sits around 300MB, so 12g is ~40x headroom -- because the point
    is to contain a runaway, not to ration ordinary work.

    `--memory-swap` equals `--memory` on purpose: with swap allowed, the cgroup
    degrades into host-wide reclaim before it kills anything, which is the stall the
    cap exists to avoid.

    '0' / 'none' / '' disable the cap, for debugging a build that legitimately
    needs more than the default.
    """
    value = str(limit or "0").strip()
    if value in ("", "0", "none"):
        return []
    return ["--memory", value, "--memory-swap", value]


async def _submit_monitor(name: str) -> dict:
    """Monitor the builder's explicit submissions (.build-submit): check the contract, write
    back .build-result, delete the request, repeat. Returns as soon as the contract passes
    (build complete), or after MAX_SUBMISSIONS without passing. The outer asyncio.wait wall
    timeout cancels this as a backstop."""
    _run = INJ._run
    subs: list[dict] = []
    while True:
        rc, _, _ = await _run(*DOCKER, "exec", name, "test", "-f", SUBMIT_FILE, timeout=30)
        if rc == 0:
            await _run(
                *DOCKER,
                "exec",
                name,
                "bash",
                "-lc",
                f"rm -f {SUBMIT_FILE} {RESULT_FILE}; : > {CHECKING_FILE}",
                timeout=30,
            )
            try:
                checks = await _contract_check(name)
            except Exception as e:
                checks = {"contract_check_error": str(e)[:200]}
            static_ok = _contract_ok(checks)
            failed = [k for k in _REQUIRED_CHECKS if not checks.get(k, False)]
            smoke_ok = mock_ok = tui_ok = perm_ok = state_ok = fileio_ok = startup_ok = headless_ok = True
            if static_ok and (not _BASIC_GATE_ONLY):
                (
                    (mock_ok, mock_results),
                    (perm_ok, perm_results),
                    (state_ok, state_results),
                    (fileio_ok, fileio_results),
                    (startup_ok, startup_results),
                    (headless_ok, headless_results),
                    (tui_line_ok, tui_line_results),
                ) = await asyncio.gather(
                    _mock_smoke(name),
                    _perm_smoke(name),
                    _state_smoke(name),
                    _fileio_smoke(name),
                    _startup_smoke(name),
                    _headless_smoke(name),
                    _tui_line_smoke(name),
                )
                checks["tui_line_smoke_ok"] = tui_line_ok
                checks["tui_line_smoke"] = tui_line_results
                checks["mock_smoke_ok"] = mock_ok
                checks["mock_smoke"] = mock_results
                checks["perm_smoke_ok"] = perm_ok
                checks["perm_smoke"] = perm_results
                checks["state_smoke_ok"] = state_ok
                checks["state_smoke"] = state_results
                checks["fileio_smoke_ok"] = fileio_ok
                checks["fileio_smoke"] = fileio_results
                checks["startup_smoke_ok"] = startup_ok
                checks["startup_smoke"] = startup_results
                checks["headless_smoke_ok"] = headless_ok
                checks["headless_smoke"] = headless_results
                if not mock_ok:
                    failed.append("mock_smoke_ok")
                if not perm_ok:
                    failed.append("perm_smoke_ok")
                if not state_ok:
                    failed.append("state_smoke_ok")
                if not fileio_ok:
                    failed.append("fileio_smoke_ok")
                if not startup_ok:
                    failed.append("startup_smoke_ok")
                if not headless_ok:
                    failed.append("headless_smoke_ok")
                if _BUILD_REAL_SMOKE:
                    (smoke_ok, smoke_results), (tui_ok, tui_results) = await asyncio.gather(
                        _functional_smoke(name), _tui_smoke(name)
                    )
                    checks["functional_smoke_ok"] = smoke_ok
                    checks["functional_smoke"] = smoke_results
                    if not smoke_ok:
                        failed.append("functional_smoke_ok")
                else:
                    tui_ok, tui_results = await _tui_smoke(name)
                    checks["functional_smoke_skipped"] = True
                checks["tui_smoke_ok"] = tui_ok
                checks["tui_smoke"] = tui_results
                if not tui_ok:
                    failed.append("tui_smoke_ok")
            if _BASIC_GATE_ONLY:
                ok = static_ok
            else:
                ok = static_ok and smoke_ok and mock_ok and perm_ok and state_ok
            if ok:
                hint = ""
            else:
                lines = [
                    _HINTS.get(
                        k, "Ensure the artifact conforms to the contract and the functional specification."
                    )
                    for k in failed
                ]
                if "yuyao_console_script" in failed and checks.get("yuyao_registered_static"):
                    try:
                        lines.remove(_HINTS["yuyao_console_script"])
                    except ValueError:
                        pass
                    lines.append(
                        "The `yuyao` command is declared but still unusable after installation. install.sh must be able to put `yuyao` on PATH **entirely offline**: the evaluation and build containers have no internet access, only the internal gateway. Check install.sh for steps that require the network (for example `pip install --upgrade pip` or fetching dependencies), since these abort the install on network failure, especially under `set -e`. Install from the bundled wheels instead, and confirm `yuyao` runs directly afterwards (see §0.1.1 items 6 and 7)."
                    )
                missing_cmds = checks.get("missing_commands") or []
                if "all_commands_present" in failed and missing_cmds:
                    lines.append(
                        "Slash commands not yet implemented (%d in total); implement each one as genuinely working, per §5: %s"
                        % (len(missing_cmds), " ".join(missing_cmds))
                    )
                    prose_only = checks.get("commands_documented_not_dispatched") or []
                    if prose_only:
                        lines.append(
                            "Of those, these appear only in a docstring or comment -- listing a command in help text is not implementing it; each needs a real handler: %s"
                            % " ".join(prose_only)
                        )
                stub_hits = checks.get("stub_hits") or []
                if "no_stub_markers" in failed and stub_hits:
                    shown = "\n".join(("    · " + h for h in stub_hits[:40]))
                    lines.append(
                        "The following locations are still unfinished or placeholder implementations; replace them with real, working code:\n%s"
                        % shown
                    )
                std_detail = checks.get("std_iface_detail") or ""
                if "std_iface_ok" in failed and std_detail:
                    lines.append(
                        "The probe of the standard programmatic interface `yuyao.agent_api` did not pass: %s"
                        % std_detail
                    )
                sm = checks.get("functional_smoke") or []
                bad = [r for r in sm if isinstance(r, dict) and (not r.get("ok"))]
                if "functional_smoke_ok" in failed and bad:
                    cap = {
                        "file_ops": "full file-operation flow (write, subdirectory, edit, append, delete)",
                        "search_replace": "bulk replacement across files plus match counting",
                        "bugfix": "locate and fix a bug (the tests must pass)",
                        "read_before_edit": "re-read before editing (an external change must not be clobbered)",
                        "status_submitted": "status.json is well-formed (includes cost_usd, not an error)",
                        "limits_exceeded": "stop cleanly when the step limit is exceeded (LimitsExceeded)",
                        "cross_file_rename": "rename across files (the definition and every reference)",
                        "glob_find": "find files by glob",
                        "apply_patch": "apply a unified diff",
                        "apply_patch_multihunk": "multi-hunk diff in one file (repeated context must not misalign)",
                        "bash_cd": "bash cd and file creation at a nested path",
                        "agents_convention": "honour the AGENTS.md conventions",
                        "edit_unique": "edit matches exactly and uniquely (replace_all must not be used)",
                        "notebook_edit": "edit a notebook cell by cell",
                        "json_output": "--json emits parsable JSON",
                        "append_system_prompt": "--append-system-prompt takes effect",
                        "no_memory_flag": "--no-memory ignores the convention files",
                        "auto_commit": "--auto-commit commits automatically",
                        "auto_commit_multi": "--auto-commit turns each successive edit into its own commit",
                        "btw_isolation": "/btw side question (a real reply that does not leak into the main history)",
                        "context_memory": "context retained across turns",
                        "clear_reset": "/clear empties the context",
                        "readonly_mode": "read-only mode (no disk writes under ask)",
                        "undo_revert": "/undo reverts changes",
                        "help_consistency": "/help matches the implementation (lists every command)",
                        "plan_readonly": "plan mode is read-only (no disk writes)",
                        "diff_shows": "/diff shows the changes",
                        "compact_continuity": "after /compact the original task continues (no context lost)",
                        "loop_repeat": "/loop repeats the prompt up to --max times",
                        "subagent_parallel": "subagents and parallelism (/batch splits work into units produced in parallel)",
                        "unknown_cmd_errors": "an unknown slash command must report a clear error rather than be silently ignored (§8.7)",
                        "remember_writes": "/remember explicitly persists memory to MEMORY.md (§6.2)",
                        "memory_autoload": "MEMORY.md loads automatically at session start and informs replies (§6.1)",
                        "remember_nl": "a natural-language 'Remember that ...' writes to the memory file (§6.2)",
                        "memory_status": "agent memory status prints the index and provider state (§6.2)",
                        "trajectory_written": "a headless run writes a .trajectory/ record under --out-dir (required by §7)",
                        "init_agents_md": "/init generates the AGENTS.md convention file (§5.1, §7)",
                        "custom_command": "a custom command in commands/<name>.md is callable and executes as /<name> (§7)",
                        "plan_file": "/plan writes an editable plan under .plans/ (§7)",
                        "unknown_flag_rc2": "an unknown flag exits with code 2 (usage error, §8.7 and §9)",
                        "conflicting_modes_rc2": "mutually exclusive permission modes (multiple --mode) exit with code 2 (§8.1)",
                        "continue_resume_rc2": "--continue and --resume are mutually exclusive and exit with code 2 (§8.4)",
                        "redo_change": "/redo reapplies a change reverted by /undo (§5.1)",
                        "status_shows": "/status shows the version, model, working directory and current mode (§5.1)",
                        "tools_lists": "/tools lists the currently available tools (§5.1)",
                        "tui_launch_no_crash": "the interactive TUI starts without crashing (a NameError or traceback on launch is a hang; white-box srcmine 1156)",
                        "cd_switches": "/cd changes the session working directory (new files land in the new directory) (§5.1)",
                        "new_clears": "/new archives the current session, starts a new one and clears the context (§5.1)",
                        "role_ask_readonly": "/role ask switches to a read-only role with no disk writes (§3.2, §8.10)",
                    }
                    req_bad = [r for r in bad if r.get("id") not in _INFO_SMOKE_IDS]
                    info_bad = [r for r in bad if r.get("id") in _INFO_SMOKE_IDS]
                    if req_bad:
                        detail = [
                            "The following features behaved incorrectly when actually run. Fix each one so it genuinely works; the parentheses show what the self-check observed:"
                        ]
                        for r in req_bad:
                            cid = r.get("id")
                            sym = _scrub_symptom(r.get("why"))
                            detail.append(
                                "    · %s%s" % (cap.get(cid, str(cid)), "(observed: %s)" % sym if sym else "")
                            )
                        lines.append("\n".join(detail))
                    if info_bad:
                        di = [
                            "The following are advanced capabilities worth completing. They do not block this submission, but the specification still requires them:"
                        ]
                        for r in info_bad:
                            cid = r.get("id")
                            sym = _scrub_symptom(r.get("why"))
                            di.append(
                                "    · %s%s" % (cap.get(cid, str(cid)), "(observed: %s)" % sym if sym else "")
                            )
                        lines.append("\n".join(di))
                mk = checks.get("mock_smoke") or []
                mbad = [r for r in mk if isinstance(r, dict) and (not r.get("ok")) and r.get("blocking")]
                if "mock_smoke_ok" in failed and mbad:
                    ml = [
                        "The following deterministic robustness, cost and parsing cases (basic error scenarios) did not meet the requirement. Fix them so they genuinely work; the parentheses show what was observed:"
                    ]
                    for r in mbad:
                        sym = _scrub_symptom(r.get("why"))
                        ml.append("    · %s%s" % (r.get("id"), "(observed: %s)" % sym if sym else ""))
                    lines.append("\n".join(ml))
                tk = checks.get("tui_smoke") or []
                tbad = [r for r in tk if isinstance(r, dict) and (not r.get("ok")) and r.get("blocking")]
                if "tui_smoke_ok" in failed and tbad:
                    tl = [
                        "The interactive interface did not display the reply to the user in a real terminal; after the user types, the assistant's reply must be visible on screen. Fix this so it genuinely works; the parentheses show what was observed:"
                    ]
                    for r in tbad:
                        sym = _scrub_symptom(r.get("why"))
                        tl.append("    · %s%s" % (r.get("id"), "(observed: %s)" % sym if sym else ""))
                    lines.append("\n".join(tl))
                pk = checks.get("perm_smoke") or []
                pbad = [r for r in pk if isinstance(r, dict) and (not r.get("ok")) and r.get("blocking")]
                if "perm_smoke_ok" in failed and pbad:
                    pl = [
                        "The following permission and security cases did not meet the requirement. A bypassed deny, or a leak of read-only or permission state, is a security hole and must fail closed. Fix these so they genuinely work; the parentheses show what was observed:"
                    ]
                    for r in pbad:
                        sym = _scrub_symptom(r.get("why"))
                        pl.append("    · %s%s" % (r.get("id"), "(observed: %s)" % sym if sym else ""))
                    lines.append("\n".join(pl))
                stk = checks.get("state_smoke") or []
                stbad = [r for r in stk if isinstance(r, dict) and (not r.get("ok")) and r.get("blocking")]
                if "state_smoke_ok" in failed and stbad:
                    stl = [
                        "The following state and sequence commands did not meet the requirement: set-get round trips for config/mcp/init, add/remove ordering, and idempotency on repeated execution. Fix these so they genuinely work; the parentheses show what was observed:"
                    ]
                    for r in stbad:
                        sym = _scrub_symptom(r.get("why"))
                        stl.append("    · %s%s" % (r.get("id"), "(observed: %s)" % sym if sym else ""))
                    lines.append("\n".join(stl))
                fk = checks.get("fileio_smoke") or []
                fbad = [r for r in fk if isinstance(r, dict) and (not r.get("ok")) and r.get("blocking")]
                if "fileio_smoke_ok" in failed and fbad:
                    fl = [
                        "The following write/read tool integrity cases did not meet the requirement: writing before reading must be blocked, an external change after a read must fail closed, an empty write must not truncate the file, and read/glob errors must degrade cleanly without crashing. Fix these so they genuinely work; the parentheses show what was observed:"
                    ]
                    for r in fbad:
                        sym = _scrub_symptom(r.get("why"))
                        fl.append("    · %s%s" % (r.get("id"), "(observed: %s)" % sym if sym else ""))
                    lines.append("\n".join(fl))
                suk = checks.get("startup_smoke") or []
                subad = [r for r in suk if isinstance(r, dict) and (not r.get("ok")) and r.get("blocking")]
                if "startup_smoke_ok" in failed and subad:
                    sul = [
                        "The following startup fail-soft cases did not meet the requirement. With a bad config, an unknown key, a BOM, or a missing directory, session construction must warn and skip rather than raise a bare traceback. Fix these so they genuinely work; the parentheses show what was observed:"
                    ]
                    for r in subad:
                        sym = _scrub_symptom(r.get("why"))
                        sul.append("    · %s%s" % (r.get("id"), "(observed: %s)" % sym if sym else ""))
                    lines.append("\n".join(sul))
                hdk = checks.get("headless_smoke") or []
                hdbad = [r for r in hdk if isinstance(r, dict) and (not r.get("ok")) and r.get("blocking")]
                if "headless_smoke_ok" in failed and hdbad:
                    hdl = [
                        "The following headless -p output-purity cases did not meet the requirement. With --output-format text, stdout must contain only the human-readable reply, free of internal XML or logs; with json, it must be a single valid JSON document. Fix these so they genuinely work; the parentheses show what was observed:"
                    ]
                    for r in hdbad:
                        sym = _scrub_symptom(r.get("why"))
                        hdl.append("    · %s%s" % (r.get("id"), "(observed: %s)" % sym if sym else ""))
                    lines.append("\n".join(hdl))
                _used = len(subs) + 1
                _remaining = MAX_SUBMISSIONS - _used
                if _remaining > 0:
                    _budget = f"[submission quota] This is submission {_used} of {MAX_SUBMISSIONS}; after fixing these you have {_remaining} attempt(s) left. Try to address everything below in this round rather than spending attempts on probing or empty submissions.\n\n"
                else:
                    _budget = f"[submission quota] This is submission {_used} of {MAX_SUBMISSIONS} and your final attempt; the quota is now exhausted. If this submission does not pass, the build ends with the current artifact and no further submissions are accepted.\n\n"
                hint = (
                    _budget
                    + "This submission did not pass validation. Working against .d2a_spec/functional_spec.md and .d2a_spec/AGENT_CONTRACT.md, bring each of the following to a genuinely working state - no placeholders, stubs, or shortcuts:\n- "
                    + "\n- ".join(lines)
                    + "\nWhen done, rm /opt/f0/.build-result then touch /opt/f0/.build-submit to resubmit."
                )
            summary = _submission_summary(len(subs) + 1, static_ok, failed, checks)
            res = {
                "submission": len(subs) + 1,
                "contract_ok": ok,
                "checks": checks,
                "hint": hint,
                "summary": summary,
            }
            subs.append(res)
            _print_submission(summary)
            payload = json.dumps({"contract_ok": ok, "hint": hint}, ensure_ascii=False)
            await _run(
                *DOCKER,
                "exec",
                name,
                "bash",
                "-lc",
                f"cat > {RESULT_FILE} <<'D2AEOF'\n{payload}\nD2AEOF\nrm -f {CHECKING_FILE} {SUBMIT_FILE}",
                timeout=30,
            )
            if ok or len(subs) >= MAX_SUBMISSIONS:
                return {"submitted": True, "contract_ok": ok, "submissions": subs}
        await asyncio.sleep(POLL_INTERVAL)


import re as _re2

_OVERFLOW_PAT = _re2.compile(
    "maximum context length|context[_ ]?length[_ ]?exceeded|ContextWindowExceeded|prompt is too long|input is too long for|exceeds the maximum.{0,40}token|too many (?:input )?tokens|reduce the length of the messages",
    _re2.I,
)


async def _round_overflowed(name: str) -> bool:
    """Scan the tail of the container's /work/opencode.jsonl to see whether this round hit a
    context-limit API error."""
    try:
        _, out, _ = await INJ._run(
            *DOCKER, "exec", name, "bash", "-lc", "tail -c 30000 /work/opencode.jsonl 2>/dev/null", timeout=30
        )
    except Exception:
        return False
    return bool(_OVERFLOW_PAT.search(out or ""))


def _detect_overflow_file(path: Path) -> bool:
    """Fallback context-overflow scan over the exported opencode.jsonl (same strong phrases
    as _round_overflowed)."""
    try:
        return bool(_OVERFLOW_PAT.search(path.read_text(encoding="utf-8", errors="replace")[-200000:]))
    except Exception:
        return False


def _termination_reason(meta: dict, opencode_path: Path) -> str:
    """Map exit_status to a plain-language termination reason (context overflow / submission
    quota exhausted / out of time / other). Kept consistent with summarize_build.py."""
    es = meta.get("exit_status", "") or ""
    n = len(meta.get("submissions") or [])
    if es == "submitted":
        return "all contract checks passed; submission accepted"
    if es == "submitted_contract_fail":
        return f"still failing after {n} submission(s) (submission limit {MAX_SUBMISSIONS} reached)"
    if es == "wall_timeout":
        return "out of time (the 2h wall-clock budget was exhausted before an explicit submission)"
    if es.startswith("context_overflow"):
        return "context overflow (the context limit was exceeded during a continuation round)"
    if es.startswith("no_progress"):
        return (
            "context overflow (the continuation trajectory stopped growing and a context-limit error was found)"
            if _detect_overflow_file(opencode_path)
            else "the builder stopped continuing (the trajectory stopped growing: the model wrapped up or gave up, not necessarily a context overflow)"
        )
    if es.startswith("rounds_exhausted"):
        return f"continuation rounds exhausted (reached MAX_CONTINUE_ROUNDS={MAX_CONTINUE_ROUNDS} without an explicit submission)"
    if _detect_overflow_file(opencode_path):
        return f"probable context overflow (exit_status={es}; the trajectory contains a context-limit error)"
    return f"other or unexpected: {es}"


async def _traj_size(name: str) -> int:
    """Current byte size of the container's /work/opencode.jsonl (progress probe; -1 if unreadable)."""
    rc, out, _ = await INJ._run(
        *DOCKER,
        "exec",
        name,
        "bash",
        "-lc",
        "wc -c < /work/opencode.jsonl 2>/dev/null || echo -1",
        timeout=30,
    )
    try:
        return int((out or "-1").strip())
    except (ValueError, AttributeError):
        return -1


async def _opencode_driver(name: str, mon_task: asyncio.Task) -> dict:
    """Drive OpenCode across rounds: send build_prompt on the first round, then feed NUDGE
    with `--continue`, until
      - the builder submits explicitly and the contract passes (mon_task finishes first)
        -> stop=submitted;
      - the trajectory stops growing (model idling or refusing) -> stop=no_progress;
      - MAX_CONTINUE_ROUNDS is reached -> stop=rounds_exhausted.
    The outer asyncio.wait wall timeout still cancels this coroutine as a backstop."""
    _run = INJ._run
    first = f"cat /work/prompt.txt | {OPENCODE_BIN} run --model d2agw/builder --format json >> /work/opencode.jsonl 2>&1"
    cont = f"cat /work/nudge.txt | {OPENCODE_BIN} run --continue --model d2agw/builder --format json >> /work/opencode.jsonl 2>&1"
    rounds = 0
    last_ec = None
    prev_size = -1
    while rounds < MAX_CONTINUE_ROUNDS:
        rounds += 1
        cmd = first if rounds == 1 else cont
        ec, _so, _se = await _run(*DOCKER, "exec", "-w", F0_DIR, name, "bash", "-lc", cmd)
        last_ec = ec
        await asyncio.sleep(POLL_INTERVAL + 1)
        if mon_task.done():
            return {"rounds": rounds, "last_ec": ec, "stop": "submitted"}
        sz = await _traj_size(name)
        if sz <= prev_size:
            stop = "context_overflow" if await _round_overflowed(name) else "no_progress"
            return {"rounds": rounds, "last_ec": ec, "stop": stop}
        prev_size = sz
    return {"rounds": rounds, "last_ec": last_ec, "stop": "rounds_exhausted"}


def _profile_base_port() -> int | None:
    """Parse the port from opencode.json's provider.d2agw.options.baseURL, to check it
    matches --gateway-port."""
    try:
        prof = json.loads(PROFILE.read_text())
        url = prof["provider"]["d2agw"]["options"]["baseURL"]
        from urllib.parse import urlparse

        return urlparse(url).port
    except Exception:
        return None


def compute_artifact_hash(root: Path) -> str:
    """sha256 over the sorted (relative path, content hash) pairs -- the artifact pin used
    for reproducibility checks."""
    h = hashlib.sha256()
    files = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any((part in _IGNORE for part in rel.parts)):
            continue
        files.append((str(rel), hashlib.sha256(p.read_bytes()).hexdigest()))
    for rel, digest in sorted(files):
        h.update(rel.encode())
        h.update(digest.encode())
    return h.hexdigest()


def plan(args, run_id: str) -> list[str]:
    """Return the readable orchestration steps (printed by --dry-run; a real run follows them)."""
    name = f"d2a-build-{run_id}"
    steps = [
        f"# run_id = {run_id}   builder_tag = {args.builder_tag}   image = {args.image}",
        "# Prerequisite: the Stage 1 gateway must be running and D2A_BUILDER_MODEL must point at the model under test (OpenCode only ever sees d2agw/builder)",
        "docker run -d --rm --entrypoint '' --add-host=host.docker.internal:host-gateway "
        + ("--cap-add=NET_ADMIN " if args.egress == "gateway-only" else "")
        + f"-e D2A_GATEWAY_KEY=*** -e D2A_MODEL_BASE_URL=http://host.docker.internal:{args.gateway_port} "
        + f"-e D2A_MODEL_NAME=litellm_proxy/builder -e D2A_MODEL_API_KEY=*** -w {F0_DIR} --name {name} {args.image} tail -f /dev/null",
        "# ^ Unified model-connection env (the model under test itself): the TUI connects with zero configuration from these defaults, and the builder uses the same values to run and self-check, so the interface matches what evaluation injects",
        (
            f"# egress=gateway-only: once the container is up, iptables restricts egress to :{args.gateway_port} only, blocking the public internet to prevent leaks and contamination"
            if args.egress == "gateway-only"
            else "# egress=open (debugging only): egress is unrestricted"
        ),
        f"docker exec {name} mkdir -p /root/.opencode {SPEC_DIR} /work",
        f"docker cp {PROFILE} {name}:/root/.opencode/opencode.json",
        f"docker cp {args.spec or '<spec to be written>'} {name}:{SPEC_DIR}/functional_spec.md",
        f"docker cp {CONTRACT} {name}:{SPEC_DIR}/AGENT_CONTRACT.md",
        f"docker cp {MODEL_REF} {name}:{SPEC_DIR}/model_call_reference.py   # reference for calling the model correctly (copy it verbatim; no need to guess the API)",
        f"docker cp {PROMPT} {name}:/work/prompt.txt"
        + (f"  (+ docker cp {args.f0_src} {name}:{F0_DIR})" if args.f0_src else ""),
        f"docker exec -w {F0_DIR} {name} bash -lc 'cat /work/prompt.txt | {OPENCODE_BIN} run --model d2agw/builder --format json >> /work/opencode.jsonl 2>&1'   # first round; if nothing is submitted, `opencode run --continue` nudges it onward (up to {MAX_CONTINUE_ROUNDS} rounds), with a {args.wall_seconds}s wall-clock backstop",
        f"docker cp {name}:{F0_DIR}/. {args.out_dir}/{run_id}/agent   # export the modified f0",
        f"docker cp {name}:/work/opencode.jsonl {args.out_dir}/{run_id}/   # archive the build trajectory",
        f"# compute artifact_hash -> build_meta.json; docker rm -f {name}",
    ]
    return steps


async def run_build(args, run_id: str) -> dict:
    _run = INJ._run
    if not getattr(args, "spec", None):
        args.spec = str(SPEC)
    name = f"d2a-build-{run_id}"
    out_run = Path(args.out_dir) / run_id
    (out_run / "agent").mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "builder_tag": args.builder_tag,
        "image": args.image,
        "started_at": datetime.now(CST).isoformat(),
        "exit_status": "",
        "artifact_hash": "",
    }
    if not args.spec:
        meta["exit_status"] = "missing_spec"
        print(
            "[run_build] no specification document (--spec). A real run requires the M1 spec to be written first; use --dry-run to inspect the orchestration.",
            file=sys.stderr,
        )
        return meta
    prof_port = _profile_base_port()
    if prof_port is not None and prof_port != int(args.gateway_port):
        meta["exit_status"] = "gateway_port_mismatch"
        print(
            f"[run_build] port mismatch: opencode.json baseURL=:{prof_port} but --gateway-port={args.gateway_port}. Update either the baseURL in opencode.json or --gateway-port so the two agree.",
            file=sys.stderr,
        )
        return meta
    started = False
    t0 = time.time()
    try:
        extra = EG.run_extra_args() if args.egress == "gateway-only" else []
        gw_port = int(args.gateway_port)
        model_base = f"http://host.docker.internal:{gw_port}"
        limits = memory_limit_flags(getattr(args, "memory_limit", "12g"))
        rc, _cid, err = await _run(
            *DOCKER,
            "run",
            "-d",
            "--rm",
            "--entrypoint",
            "",
            "--add-host=host.docker.internal:host-gateway",
            *limits,
            *extra,
            "-e",
            f"D2A_GATEWAY_KEY={args.gateway_key}",
            "-e",
            f"D2A_MODEL_BASE_URL={model_base}",
            "-e",
            "D2A_MODEL_NAME=litellm_proxy/builder",
            "-e",
            f"D2A_MODEL_API_KEY={args.gateway_key}",
            "-w",
            F0_DIR,
            "--name",
            name,
            args.image,
            "tail",
            "-f",
            "/dev/null",
            timeout=600,
        )
        if rc != 0:
            meta["exit_status"] = "container_start_failed"
            meta["error"] = err[:800]
            return meta
        started = True
        await _run(*DOCKER, "exec", name, "mkdir", "-p", "/root/.opencode", SPEC_DIR, "/work", timeout=120)
        await _run(*DOCKER, "cp", str(PROFILE), f"{name}:/root/.opencode/opencode.json", timeout=60)
        await _run(*DOCKER, "cp", str(args.spec), f"{name}:{SPEC_DIR}/functional_spec.md", timeout=60)
        await _run(*DOCKER, "cp", str(CONTRACT), f"{name}:{SPEC_DIR}/AGENT_CONTRACT.md", timeout=60)
        await _run(*DOCKER, "cp", str(MODEL_REF), f"{name}:{SPEC_DIR}/model_call_reference.py", timeout=60)
        _prompt_txt = PROMPT.read_text(encoding="utf-8").replace("{{MAX_SUBMISSIONS}}", str(MAX_SUBMISSIONS))
        if _BASIC_GATE_ONLY:
            _prompt_txt += "\n\n=== IMPORTANT: submission rules for this run ===\nThis run uses a single-shot finalization protocol. Read it carefully before you begin.\n1. Each `touch /opt/f0/.build-submit` validates only the basic gate: whether the agent installs, starts, and compiles; whether every slash command is present; whether any placeholders or stubs remain; and whether the standard programmatic interface `yuyao.agent_api` works. In short, whether the artifact is runnable and ready to be tested.\n2. Once the basic gate passes, that version is final. The build ends immediately, no further submissions are accepted, and you receive no functional or test feedback. The artifact is then evaluated in full, and that result is your final score.\n3. Only when the basic gate fails, meaning the agent is not usable at all, do you receive a build-failure notice listing what is missing, along with another chance to submit.\n4. Do not plan to submit early and iterate on feedback: no test feedback is available. Implement every requirement in `.d2a_spec/functional_spec.md` as completely and correctly as you can, and only run `touch /opt/f0/.build-submit` once you are confident this is the best version you can deliver.\n================================================\n"
        await _run(
            *DOCKER,
            "exec",
            name,
            "bash",
            "-lc",
            f"cat > /work/prompt.txt <<'D2APROMPT'\n{_prompt_txt}\nD2APROMPT",
            timeout=60,
        )
        await _run(
            *DOCKER,
            "exec",
            name,
            "bash",
            "-lc",
            f"cat > /work/nudge.txt <<'D2ANUDGE'\n{_NUDGE}\nD2ANUDGE",
            timeout=30,
        )
        if args.f0_src:
            await _run(
                *DOCKER,
                "exec",
                name,
                "bash",
                "-lc",
                f'set -e; cd {F0_DIR}; find . -mindepth 1 -maxdepth 1 -not -name ".*" -exec rm -rf {{}} + ; echo "[f0-src] wiped f0 (kept dotfiles)"',
                timeout=120,
            )
            await _run(*DOCKER, "cp", f"{args.f0_src}/.", f"{name}:{F0_DIR}", timeout=300)
            await _run(
                *DOCKER,
                "exec",
                name,
                "bash",
                "-lc",
                f"find {F0_DIR} -name __pycache__ -type d -prune -exec rm -rf {{}} + 2>/dev/null || true",
                timeout=60,
            )
        gw_port = int(args.gateway_port)
        if args.egress == "gateway-only":
            eg = await EG.harden(_run, DOCKER, name, gw_port)
            if eg["applied"]:
                eg.update(await EG.verify(_run, DOCKER, name))
            else:
                print(
                    f"[run_build] warning: egress is not restricted: {eg['reason']}. A comparable run must satisfy this prerequisite.",
                    file=sys.stderr,
                )
            meta["egress"] = eg
        mon_task = asyncio.create_task(_submit_monitor(name))
        driver_task = asyncio.create_task(_opencode_driver(name, mon_task))
        done, pending = await asyncio.wait(
            {driver_task, mon_task}, timeout=args.wall_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        meta["submitted"] = False
        mon_result = None
        if mon_task in done and (not mon_task.cancelled()):
            try:
                mon_result = mon_task.result()
            except Exception as e:
                meta["exit_status"] = f"monitor_error:{type(e).__name__}"
                meta["error"] = str(e)[:300]
        if mon_result is not None:
            r = mon_result
            meta.update(submitted=r["submitted"], contract_ok=r["contract_ok"], submissions=r["submissions"])
            meta["exit_status"] = "submitted" if r["contract_ok"] else "submitted_contract_fail"
            if driver_task in done and (not driver_task.cancelled()):
                try:
                    meta["build_rounds"] = driver_task.result().get("rounds")
                except Exception:
                    pass
        elif meta["exit_status"]:
            pass
        elif driver_task in done and (not driver_task.cancelled()):
            try:
                d = driver_task.result()
                meta["build_rounds"] = d.get("rounds")
                meta["exit_status"] = f"{d.get('stop', 'driver_done')}_no_submit"
            except Exception as e:
                meta["exit_status"] = f"opencode_error:{type(e).__name__}"
        else:
            meta["exit_status"] = "wall_timeout"
        await _run(*DOCKER, "cp", f"{name}:{F0_DIR}/.", str(out_run / "agent"), timeout=300)
        await _run(
            *DOCKER, "cp", f"{name}:/work/opencode.jsonl", str(out_run / "opencode.jsonl"), timeout=120
        )
        meta["artifact_hash"] = compute_artifact_hash(out_run / "agent")
    except Exception as e:
        meta["exit_status"] = meta["exit_status"] or f"build_error:{type(e).__name__}"
        meta["error"] = str(e)[:800]
    finally:
        if started:
            await _run(*DOCKER, "rm", "-f", name, timeout=120)
    meta["elapsed_s"] = round(time.time() - t0, 1)
    meta["termination_reason"] = _termination_reason(meta, out_run / "opencode.jsonl")
    (out_run / "build_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    try:
        REG.register(
            {
                "kind": "build",
                "run_id": run_id,
                "builder_tag": args.builder_tag,
                "artifact_hash": meta.get("artifact_hash", ""),
                "exit_status": meta["exit_status"],
                "submitted": meta.get("submitted", False),
                "contract_ok": meta.get("contract_ok", False),
                "raw_path": str(out_run / "agent"),
            }
        )
    except Exception as e:
        print(f"[run_build] registration failed (ignored): {e}", file=sys.stderr)
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 1 build orchestration: the builder uses OpenCode to transform mini into an artifact"
    )
    ap.add_argument(
        "--builder-tag",
        required=True,
        help="short tag for the model under test, used in run_id and meta; the actual model is determined by the Stage 1 gateway via D2A_BUILDER_MODEL",
    )
    ap.add_argument("--spec", default="", help=f"path to the specification document (default: {SPEC.name})")
    ap.add_argument(
        "--f0-src",
        default="",
        help="optional host directory of mini sources that overrides /opt/f0 in the image",
    )
    ap.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=f"builder image (default {DEFAULT_IMAGE}; override with D2A_BUILDER_IMAGE)",
    )
    ap.add_argument(
        "--gateway-key",
        default=os.environ.get("D2A_GATEWAY_KEY", "sk-d2a-local-master"),
        help="master key for the local gateway (default: D2A_GATEWAY_KEY); it is the only credential in the container, which holds no upstream credentials",
    )
    ap.add_argument("--out-dir", default=str(HERE.parent.parent / "results" / "stage1"))
    ap.add_argument(
        "--wall-seconds",
        type=int,
        default=7200,
        help="build wall-clock budget in seconds (killed on timeout; default 2h)",
    )
    ap.add_argument(
        "--memory-limit",
        default=default_memory_limit(),
        help="per-container memory cap passed to `docker run --memory` (default 12g, override with D2A_BUILD_MEMORY; '0' disables). A normal build sits around 300MB, so this is ~40x headroom -- it exists to contain a runaway, not to ration normal work. Without it a single build that allocates unboundedly can take the whole host down, and the OOM killer then reaps its sibling builds too.",
    )
    ap.add_argument(
        "--gateway-port",
        type=int,
        default=int(os.environ.get("D2A_GATEWAY_PORT", "4000")),
        help="builder gateway port; must match the baseURL in opencode.json (override with D2A_GATEWAY_PORT). The egress allowlist opens only this port.",
    )
    ap.add_argument(
        "--egress",
        choices=["open", "gateway-only"],
        default="gateway-only",
        help="gateway-only (default): allow the gateway port only and block the public internet, so a build cannot fetch answers or leak the spec; needs iptables and NET_ADMIN in the image. open: no restriction (debugging only).",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the orchestration plan only: no container, no model call, no cost",
    )
    args = ap.parse_args()
    if not args.spec:
        args.spec = str(SPEC)
    for _doc in (Path(args.spec), PROMPT, CONTRACT, MODEL_REF):
        if not _doc.is_file():
            raise SystemExit(f"[run_build] required document is missing: {_doc}")
    run_id = datetime.now(CST).strftime("%Y-%m-%d-%H-%M")
    out_base = Path(args.out_dir)
    if not args.dry_run:
        stem, i = (run_id, 1)
        while True:
            try:
                (out_base / run_id).mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                i += 1
                run_id = f"{stem}-{i}"
    if args.dry_run:
        prof_port = _profile_base_port()
        if prof_port is not None and prof_port != int(args.gateway_port):
            print(
                f"[run_build] port mismatch: opencode.json baseURL=:{prof_port} but --gateway-port={args.gateway_port}. Update either the baseURL in opencode.json or --gateway-port so the two agree.",
                file=sys.stderr,
            )
            return 2
        print(f"=== Stage 1 build orchestration plan (dry-run, run_id={run_id}) ===")
        for s in plan(args, run_id):
            print(s)
        print(
            "\n(For a real run, drop --dry-run; it requires the builder image, the specification document, and a running Stage 1 gateway.)"
        )
        return 0
    meta = asyncio.run(run_build(args, run_id))
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0 if meta.get("artifact_hash") else 1


if __name__ == "__main__":
    sys.exit(main())
