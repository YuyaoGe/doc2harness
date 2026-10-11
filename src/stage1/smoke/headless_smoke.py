"""Headless `-p`/`--print` text/JSON output-purity determinism smoke (zero real-model budget).

Why it exists: spec §0.1.1/§8.3/§9 pin down headless mode (`yuyao -p "<task>" --output-format text`) -
stdout must contain ONLY the human-readable reply, never internal XML/function-call tags, phase events,
kernel banners, or log lines that belong on stderr; `--output-format json` must emit a SINGLE standard
JSON document (containing the reply) with no non-JSON noise. This is the hard contract for user scripts
and CI that treat the agent as a pipe.

Why it is its own layer (not part of mock_cases): mock_cases drives the EVAL-HARNESS entry
`entry.py --task-file --out-dir`, whose deliverable is status.json plus the trajectory and whose stdout
may legitimately be empty; the purity contract applies only to the USER CLI path `yuyao -p`. Judging
stdout purity on the entry.py path would produce false positives on compliant builds, so this layer
drives the real `-p` CLI.

Mechanism: start a local mock gateway (`fixed_reply` returns plain text with a sentinel and no tool
calls) -> run `yuyao -p "<task>" --output-format text|json` (model endpoint injected via `D2A_MODEL_*`)
-> capture STDOUT -> judge purity/validity.
N/A gate: a build with no `-p`/`--print` text CLI (neither the `yuyao` console script nor `python -m
yuyao` accepts -p) -> inconclusive, skipped.
Zero false positives: the control sentinel MUST appear in stdout (a missing sentinel = a no-op or
scrubbed vacuous pass; leaked internal tags = nothing was really parsed / rendering was not clean = FAIL).

Usage: python headless_smoke.py <agent_dir> [--emit-json] | --selftest
"""

from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_gateway as MG
import smoke_contract as SC

RESULT_TAG = "HEADLESS_RESULTS="


BAD = [
    "<function_calls>",
    "<tool_code>",
    "<invoke",
    "<pre_dispatch_explanation>",
    "<response>",
    "</response>",
    "TurnBegin",
    "StepBegin",
    "[ERROR]",
    "[INFO]",
    "Loaded cached credentials",
    "mini-swe-agent version",
    "Traceback (most recent call last)",
    "DEBUG:",
]


def _discover_cli(agent_dir: Path, *, require_print: bool = True):
    """Discover the artifact's own CLI entry.

    ``require_print=True`` requires the entry to support ``-p/--print``, for headless session cases;
    ``False`` only requires the bare entry's help to succeed, for ``memory/config/...`` management
    subcommand cases. The two capabilities must be discovered independently - a build without ``-p``
    must not swallow the bare subcommand surface.
    """
    env = dict(os.environ)
    src = os.pathsep.join([str((agent_dir / "src").resolve()), str(agent_dir.resolve())])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["MSWEA_SILENT_STARTUP"] = "1"
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["MSWEA_COST_TRACKING"] = "ignore_errors"
    cands = [[sys.executable, "-m", "yuyao"], [sys.executable, "-m", "yuyao.cli"], ["yuyao"]]
    for c in cands:
        for help_flag in ("--help", "-h"):
            try:
                r = subprocess.run(
                    c + [help_flag], cwd=str(agent_dir), env=env, capture_output=True, text=True, timeout=40
                )
            except Exception:
                continue
            help_txt = (r.stdout or "") + (r.stderr or "")
            has_print = re.search("(?<![\\w-])(?:-p|--print)(?=[\\s,=]|$)", help_txt) is not None
            help_complete = (
                re.search("(?im)^\\s*(usage|用法)[:：]", help_txt) is not None and len(help_txt) > 200
            )
            if (has_print or not require_print) and (r.returncode == 0 or help_complete):
                return (c, env)
    return (None, env)


def _json_loads_strict(text):
    """Standard JSON: reject the NaN/Infinity that Python's json tolerates by default."""

    def _bad_constant(value):
        raise ValueError(f"non-standard JSON constant: {value}")

    return json.loads(text, parse_constant=_bad_constant)


def chk_json_pure(sentinel):

    def _f(res, reqs=None):
        if not res.get("std"):
            return (True, "inconclusive: build has no -p/--print CLI -> skipped")
        if res.get("timed_out"):
            return (
                False,
                "headless -p --output-format json did not terminate within the limit (probable hang)",
            )
        out = (res.get("stdout") or "").strip()
        try:
            obj = _json_loads_strict(out)
            bad_json = False
        except Exception:
            obj = None
            bad_json = True
        leaks = [b for b in BAD if b in out]
        result_field = ""
        status_field = ""
        if isinstance(obj, dict):
            result_field = str(obj.get("result", ""))
            status_field = str(obj.get("exit_status", ""))
        has = sentinel in result_field or (
            isinstance(obj, dict) and sentinel in json.dumps(obj, ensure_ascii=False)
        )
        ok = obj is not None and (not bad_json) and has and (not leaks) and (res.get("rc") == 0)
        return (
            ok,
            f"whole text is one JSON={obj is not None and (not bad_json)} result contains sentinel={has} exit={status_field!r} leaks={leaks} rc={res.get('rc')} (-p json must be a single valid JSON containing the result sentinel, with no noise)",
        )

    return _f


def chk_json_error_pure(res, reqs=None):
    """On a deterministic model error in JSON output mode, stdout must still be a single error JSON and the process rc=1; never an empty stdout, interleaved logs, or a fake success."""
    if not res.get("std"):
        return (True, "inconclusive: build has no -p/--print CLI -> skipped")
    if res.get("timed_out"):
        return (False, "the headless JSON error branch did not terminate within the limit")
    n = len(reqs or [])
    out = (res.get("stdout") or "").strip()
    try:
        _json_loads_strict(out)
        one_json = True
    except Exception:
        one_json = False
    ok = n >= 1 and one_json and (res.get("rc") == 1)
    return (
        ok,
        f"401 really triggered={n >= 1} (reqs={n}) whole stdout is standard JSON={one_json} rc={res.get('rc')} (a model error in JSON mode must give structured output + rc1; the spec pins neither the retry count, nor the error JSON fields, nor stderr diagnostics)",
    )


def chk_probe_present(what):
    """Generic wire evidence: with cfg.probe_token=marker, any request with has_probe=True means the content reached the wire. Self-gated on reqs>=1."""

    def _f(res, reqs=None):
        if not res.get("std"):
            return (True, "inconclusive: build has no -p/--print CLI -> skipped")
        if res.get("timed_out"):
            return (False, f"{what} did not terminate within the limit")
        n = len(reqs or [])
        present = any((rec.get("has_probe") for rec in reqs or [] if isinstance(rec, dict)))
        return (
            n >= 1 and present,
            f"reqs={n} {what} in request body={present} (must be wired into the prompt sent to the model; absent from every request = discarded)",
        )

    return _f


def chk_probe_absent(what):
    """The inverse of `chk_probe_present`: this content must NOT appear in any request body.

    Self-gating mirrors the positive version (no -p CLI / zero requests => inconclusive). Without it,
    "the sentinel did not appear" would also count "no request was ever sent" as a pass - the classic
    vacuous pass.
    Used for inverse controls: proving content enters the context because it was REFERENCED, not
    because the product reads any file name it sees.
    """

    def _f(res, reqs=None):
        if not res.get("std"):
            return (True, "inconclusive: build has no -p/--print CLI -> skipped")
        n = len(reqs or [])
        if n < 1:
            return (True, "inconclusive: not a single model request was sent -> skipped")
        present = any((rec.get("has_probe") for rec in reqs or [] if isinstance(rec, dict)))
        return (not present, f"reqs={n} {what} in request body={present} (must not enter the request body)")

    return _f
_MATRIX_REPLY = "MATRIX_EXACT_REPLY_6047"
_MATRIX_REPLY_BYTES = (_MATRIX_REPLY + "\n").encode()
_MATRIX_FORMAL_ROUNDS = [
    [
        {
            "name": ["bash", "shell", "execute"],
            "args": {"command": "printf 'MATRIX_EFFECT_3719\\n' > matrix_effect_3719.txt"},
        }
    ]
]
_MATRIX_REQUIRED_FLAGS = (
    ("task-file", "--task-file"),
    ("repo-dir", "--repo-dir"),
    ("model-base-url", "--model-base-url"),
    ("model-name", "--model-name"),
    ("model-api-key", "--model-api-key"),
    ("budget-file", "--budget-file"),
    ("out-dir", "--out-dir"),
)


class _MatrixEffectHandler(MG.Handler):
    """Give the one-tool formal control a unique deterministic final answer."""

    def _send(self, status: int, body: dict):
        cfg = getattr(self.server, "cfg", {})
        if cfg.get("matrix_effect") and status == 200 and isinstance(body, dict):
            body = json.loads(json.dumps(body))
            choices = body.get("choices")
            message = (
                choices[0].get("message")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                else None
            )
            if isinstance(message, dict) and message.get("content") == "done":
                message["content"] = _MATRIX_REPLY
        return super()._send(status, body)

    def _send_sse(self, model: str, content: str):
        cfg = getattr(self.server, "cfg", {})
        if cfg.get("matrix_effect") and content == "done":
            content = _MATRIX_REPLY
        return super()._send_sse(model, content)


_CONTRACT_SUCCESS_STATUSES = frozenset({"Submitted", "LimitsExceeded"})
_CONTRACT_ASCII_TEXT = "READY-ASCII"
_CONTRACT_BUDGET_VALID = (
    {"label": "valid-zero", "budget": {"max_steps": 2, "max_tokens": 0, "max_wall_seconds": 30}},
    {"label": "valid-positive", "budget": {"max_steps": 97, "max_tokens": 64, "max_wall_seconds": 30}},
)
_CONTRACT_BUDGET_INVALID = (
    {"label": "invalid-boolean", "budget": {"max_steps": True, "max_tokens": 0, "max_wall_seconds": 30}},
    {"label": "invalid-string", "budget": {"max_steps": "2", "max_tokens": 0, "max_wall_seconds": 30}},
    {"label": "invalid-float", "budget": {"max_steps": 2.5, "max_tokens": 0, "max_wall_seconds": 30}},
    {"label": "invalid-negative", "budget": {"max_steps": -1, "max_tokens": 0, "max_wall_seconds": 30}},
)


def _contract_verdict(status: str, why: str):
    return (status in {"pass", "na"}, why, status)


def _contract_rows(res: dict, expected: tuple[str, ...]):
    error = res.get("formal_error")
    rows = tuple(res.get("formal_rows") or ())
    if error:
        return (None, f"formal headless driver error: {error}")
    labels = tuple((row.get("label") for row in rows))
    if labels != expected:
        return (None, f"formal matrix incomplete: labels={labels!r} expected={expected!r}")
    for row in rows:
        if row.get("start_error"):
            return (None, f"{row['label']}: process/setup error={row['start_error']}")
        if row.get("timed_out"):
            return (None, f"{row['label']}: process timeout")
        if not row.get("cleanup_complete"):
            return (None, f"{row['label']}: workspace cleanup incomplete")
        if type(row.get("request_count")) is not int:
            return (None, f"{row['label']}: model request count unavailable")
        if not isinstance(row.get("stdout"), bytes) or not isinstance(row.get("stderr"), bytes):
            return (None, f"{row['label']}: raw byte capture unavailable")
    return (rows, "")


def _contract_status_name(row: dict) -> str | None:
    status = row.get("status_value")
    if isinstance(status, dict) and isinstance(status.get("exit_status"), str):
        return status["exit_status"]
    return None


def _contract_statuses_complete(rows) -> bool:
    return all((not row.get("status_error") and _contract_status_name(row) is not None for row in rows))


def _contract_success(row: dict) -> bool:
    return row.get("rc") == 0 and _contract_status_name(row) in _CONTRACT_SUCCESS_STATUSES


def chk_budget_type_domain_control(res, reqs=None):
    expected = ("valid-zero", "valid-positive")
    rows, error = _contract_rows(res, expected)
    if rows is None:
        return _contract_verdict("error", error)
    if not _contract_statuses_complete(rows):
        return _contract_verdict("error", "valid budget rc/status channel incomplete")
    facts = {row["label"]: _contract_success(row) and row["request_count"] == 1 for row in rows}
    ok = all(facts.values())
    return _contract_verdict(
        "pass" if ok else "fail",
        f"zero/positive integer controls={facts} requests={[row['request_count'] for row in rows]}",
    )


def chk_budget_type_domain(res, reqs=None):
    expected = (
        "valid-zero",
        "valid-positive",
        "invalid-boolean",
        "invalid-string",
        "invalid-float",
        "invalid-negative",
    )
    rows, error = _contract_rows(res, expected)
    if rows is None:
        return _contract_verdict("error", error)
    if not _contract_statuses_complete(rows):
        return _contract_verdict("error", "budget matrix rc/status channel incomplete")
    valid = rows[:2]
    invalid = rows[2:]
    valid_ok = all((_contract_success(row) and row["request_count"] == 1 for row in valid))
    invalid_ok = all(
        (
            row.get("rc") not in (None, 0)
            and _contract_status_name(row) not in _CONTRACT_SUCCESS_STATUSES
            and (row["request_count"] == 0)
            for row in invalid
        )
    )
    ok = valid_ok and invalid_ok
    return _contract_verdict(
        "pass" if ok else "fail",
        f"valid integers accepted={valid_ok} invalid type/domain rejected pre-model={invalid_ok} rc={[row.get('rc') for row in rows]} requests={[row['request_count'] for row in rows]}",
    )


def _matrix_status_token(row: dict) -> str:
    """Return the exact public status observation, including deliberate absence."""
    error = str(row.get("status_error") or "")
    if error:
        return error
    status = row.get("status_value")
    if isinstance(status, dict) and isinstance(status.get("exit_status"), str):
        return status["exit_status"]
    return "INVALID"


def _matrix_rows(res: dict, key: str, expected: tuple[str, ...]):
    error = str(res.get("matrix_error") or "")
    rows = tuple(res.get(key) or ())
    if error:
        return (None, f"Matrix driver error: {error}")
    labels = tuple((row.get("label") for row in rows))
    if labels != expected:
        return (None, f"Matrix matrix incomplete: labels={labels!r} expected={expected!r}")
    for row in rows:
        if row.get("start_error"):
            return (None, f"{row['label']}: process/setup error={row['start_error']}")
        if not row.get("cleanup_complete"):
            return (None, f"{row['label']}: workspace cleanup incomplete")
        if type(row.get("rc")) is not int:
            return (None, f"{row['label']}: process rc unavailable")
        if type(row.get("request_count")) is not int:
            return (None, f"{row['label']}: request count unavailable")
        if not isinstance(row.get("stdout"), bytes) or not isinstance(row.get("stderr"), bytes):
            return (None, f"{row['label']}: raw stdout/stderr unavailable")
    return (rows, "")


def _matrix_exact_facts(
    row: dict, *, rc: int, requests: int, stdout: bytes, status: str | tuple[str, ...]
) -> dict:
    """Compare a row against exact expected facts.

    `status` may be a tuple when more than one status observation satisfies the
    spec.
    """
    accepted = (status,) if isinstance(status, str) else tuple(status)
    return {
        "terminated": not row.get("timed_out"),
        "rc": row.get("rc") == rc,
        "requests": row.get("request_count") == requests,
        "stdout": row.get("stdout") == stdout,
        "status": _matrix_status_token(row) in accepted,
    }


def _matrix_verdict(res: dict, key: str, expected: tuple[str, ...], oracle, subject: str):
    rows, error = _matrix_rows(res, key, expected)
    if rows is None:
        return _contract_verdict("error", error)
    facts = {row["label"]: oracle(row) for row in rows}
    ok = all((all(values.values()) for values in facts.values()))
    summary = {
        row["label"]: {
            "rc": row.get("rc"),
            "req": row.get("request_count"),
            "stdout": len(row.get("stdout") or b""),
            "status": _matrix_status_token(row),
            "timeout": bool(row.get("timed_out")),
            "oracle": facts[row["label"]],
        }
        for row in rows
    }
    return _contract_verdict("pass" if ok else "fail", f"{subject} exact rc/request/stdout/status={summary}")


def _matrix_formal_success(row: dict) -> dict:
    return _matrix_exact_facts(row, rc=0, requests=2, stdout=_MATRIX_REPLY_BYTES, status="Submitted")


def chk_runtime_error_stdout_control(res, reqs=None):
    return _matrix_verdict(
        res,
        "matrix_formal_rows",
        ("model-success",),
        _matrix_formal_success,
        "R1I-21 zero-weight successful runtime",
    )


def chk_runtime_error_stdout(res, reqs=None):

    def oracle(row):
        return _matrix_exact_facts(row, rc=1, requests=1, stdout=b"", status="Error")

    return _matrix_verdict(
        res,
        "matrix_formal_rows",
        ("model-runtime-error",),
        oracle,
        "R1I-21 runtime error has no pseudo-result stdout",
    )


def _contract_clean_env(root: Path, base_env: dict, extra: dict | None) -> dict:
    runtime = Path(base_env.get("D2A_READY_PYTHON") or sys.executable).resolve()
    path_items = (str(runtime.parent), *os.defpath.split(os.pathsep))
    env = {
        "PATH": os.pathsep.join(dict.fromkeys((item for item in path_items if item))),
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "TMP": str(root / "tmp"),
        "TEMP": str(root / "tmp"),
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8:strict",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "YUYAO_PYTHON": str(runtime),
        "MSWEA_SILENT_STARTUP": "1",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "MSWEA_COST_TRACKING": "ignore_errors",
    }
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _contract_read_status(path: Path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return (None, "MISSING")
    except OSError as exc:
        return (None, f"READ_{type(exc).__name__.upper()}")
    if len(raw) > 1 << 20:
        return (None, "TOO_LARGE")
    try:
        value = _json_loads_strict(raw.decode("utf-8", "strict"))
    except (UnicodeError, ValueError):
        return (None, "MALFORMED")
    if not isinstance(value, dict):
        return (value, "NOT_OBJECT")
    return (value, "")


def _contract_tree_snapshot(root: Path):
    """Hash files and path types without following symlinks."""
    rows = []

    def visit(directory: Path, prefix: str) -> None:
        for entry in sorted(os.scandir(directory), key=lambda item: item.name.encode()):
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            metadata = entry.stat(follow_symlinks=False)
            if entry.is_symlink():
                payload = os.readlink(entry.path).encode("utf-8", "surrogateescape")
                rows.append((relative, "symlink", hashlib.sha256(payload).hexdigest()))
            elif entry.is_dir(follow_symlinks=False):
                rows.append((relative, "directory", ""))
                visit(Path(entry.path), relative)
            elif entry.is_file(follow_symlinks=False):
                digest = hashlib.sha256()
                with open(entry.path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                rows.append((relative, "file", digest.hexdigest()))
            else:
                rows.append((relative, "other", str(metadata.st_mode)))

    visit(root, "")
    return tuple(rows)


def _drive_contract_formal(agent_dir: Path, base_url: str, cfg: dict, base_env: dict, case: dict) -> dict:
    launcher = agent_dir / "run_agent.sh"
    if not launcher.is_file():
        return {"std": True, "formal_rows": (), "formal_error": "RUN_AGENT_MISSING"}
    rows = []
    try:
        for scenario in case.get("formal_scenarios") or ():
            label = str(scenario["label"])
            root = Path(tempfile.mkdtemp(prefix=f"headless_contract_{label}_"))
            row = {
                "label": label,
                "rc": None,
                "stdout": b"",
                "stderr": b"",
                "timed_out": False,
                "cleanup_complete": True,
                "start_error": "",
                "status_value": None,
                "status_error": "MISSING",
                "request_count": None,
                "repo_changed": None,
                "out_created": None,
            }
            try:
                home, temp = (root / "home", root / "tmp")
                home.mkdir()
                temp.mkdir()
                layout = root / "paths" / label
                repo = layout / "repo"
                repo.mkdir(parents=True)
                (repo / "fixture.txt").write_bytes(b"unchanged\n")
                role = scenario.get("path_role", "sibling")
                if role == "sibling":
                    out = layout / "out"
                elif role == "direct":
                    out = repo / "out"
                elif role == "parent_alias":
                    decoy = layout / "decoy"
                    decoy.mkdir()
                    out = decoy / ".." / "repo" / "escaped-out"
                elif role == "symlink_alias":
                    alias = layout / "repo-alias"
                    alias.symlink_to(repo, target_is_directory=True)
                    out = alias / "linked-out"
                else:
                    raise ValueError(f"unknown path role {role!r}")
                fixtures = root / "fixtures"
                fixtures.mkdir()
                task_file = fixtures / "task.json"
                budget_file = fixtures / "budget.json"
                task_file.write_text(
                    json.dumps(
                        {
                            "task_id": f"contract-{label}",
                            "type": "formal-headless-oracle",
                            "problem_statement": "Return the model result without tools.",
                            "repo_dir": str(repo),
                            "allow_network": False,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                budget_file.write_text(
                    json.dumps(
                        scenario.get("budget") or {"max_steps": 2, "max_tokens": 0, "max_wall_seconds": 30},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    encoding="utf-8",
                )
                argv = [
                    "bash",
                    str(launcher),
                    "--task-file",
                    str(task_file),
                    "--repo-dir",
                    str(repo),
                    "--model-base-url",
                    base_url,
                    "--model-name",
                    "litellm_proxy/mock",
                    "--model-api-key",
                    "sk-mock-test",
                    "--budget-file",
                    str(budget_file),
                    "--out-dir",
                    str(out),
                ]
                cfg["reply_content"] = scenario.get(
                    "reply_content", case.get("reply_content", _CONTRACT_ASCII_TEXT)
                )
                before_requests = len(cfg.get("requests") or [])
                before_repo = _contract_tree_snapshot(repo)
                try:
                    completed = subprocess.run(
                        argv,
                        cwd=str(root),
                        env=_contract_clean_env(root, base_env, scenario.get("env")),
                        capture_output=True,
                        text=False,
                        timeout=case.get("timeout", 30),
                        start_new_session=True,
                    )
                    row["rc"] = completed.returncode
                    row["stdout"] = completed.stdout or b""
                    row["stderr"] = completed.stderr or b""
                except subprocess.TimeoutExpired as exc:
                    row["rc"] = -1
                    row["stdout"] = exc.stdout or b""
                    row["stderr"] = exc.stderr or b""
                    row["timed_out"] = True
                except (OSError, ValueError) as exc:
                    row["start_error"] = type(exc).__name__
                row["request_count"] = len(cfg.get("requests") or []) - before_requests
                row["status_value"], row["status_error"] = _contract_read_status(out / "status.json")
                row["repo_changed"] = before_repo != _contract_tree_snapshot(repo)
                row["out_created"] = out.exists()
            except Exception as exc:
                row["start_error"] = f"SETUP_{type(exc).__name__.upper()}"
            finally:
                try:
                    shutil.rmtree(root)
                except OSError:
                    row["cleanup_complete"] = False
            rows.append(row)
    except Exception as exc:
        return {
            "std": True,
            "formal_rows": tuple(rows),
            "formal_error": f"DRIVER_{type(exc).__name__.upper()}",
        }
    return {"std": True, "formal_rows": tuple(rows), "formal_error": ""}


def _matrix_run_process(argv, *, cwd: Path, env: dict, input_bytes: bytes, timeout: float) -> dict:
    result = {"rc": None, "stdout": b"", "stderr": b"", "timed_out": False, "start_error": ""}
    process = None
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input=input_bytes, timeout=timeout)
            result["rc"] = process.returncode
            result["stdout"] = stdout or b""
            result["stderr"] = stderr or b""
        except subprocess.TimeoutExpired as exc:
            result["timed_out"] = True
            result["stdout"] = exc.stdout or b""
            result["stderr"] = exc.stderr or b""
            try:
                os.killpg(process.pid, 9)
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                stdout, stderr = process.communicate(timeout=5)
                result["stdout"] = stdout or result["stdout"]
                result["stderr"] = stderr or result["stderr"]
            except (OSError, subprocess.TimeoutExpired):
                pass
            result["rc"] = -1
    except (OSError, ValueError) as exc:
        result["start_error"] = type(exc).__name__
    return result


def _matrix_json_object(pairs) -> bytes:
    return (
        "{"
        + ",".join(
            (
                f"{json.dumps(key, ensure_ascii=False)}:{json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
                for key, value in pairs
            )
        )
        + "}"
    ).encode()


def _drive_matrix_formal(agent_dir: Path, base_url: str, cfg: dict, base_env: dict, case: dict) -> dict:
    """Drive exact seven-flag contract matrices without importing product code."""
    launcher = agent_dir / "run_agent.sh"
    if not launcher.is_file():
        return {"std": True, "matrix_formal_rows": (), "matrix_error": "RUN_AGENT_MISSING"}
    rows = []
    driver_error = ""
    try:
        for scenario in case.get("matrix_formal_scenarios") or ():
            label = str(scenario["label"])
            root = Path(tempfile.mkdtemp(prefix=f"headless_matrix_formal_{label}_"))
            row = {
                "label": label,
                "rc": None,
                "stdout": b"",
                "stderr": b"",
                "timed_out": False,
                "cleanup_complete": True,
                "start_error": "",
                "status_value": None,
                "status_error": "MISSING",
                "request_count": None,
            }
            try:
                home, temp = (root / "home", root / "tmp")
                home.mkdir()
                temp.mkdir()
                repo = root / "repo"
                repo.mkdir()
                (repo / "fixture.txt").write_bytes(b"unchanged\n")
                out = root / "out"
                fixtures = root / "fixtures"
                fixtures.mkdir()
                task_file = fixtures / "task.json"
                budget_file = fixtures / "budget.json"
                task = {
                    "task_id": f"matrix-{label}",
                    "type": "formal-headless-oracle",
                    "problem_statement": "Return the fixed Matrix reply.",
                    "repo_dir": str(repo),
                    "allow_network": False,
                }
                task.update(dict(scenario.get("task_overrides") or {}))
                budget = {"max_steps": 4, "max_tokens": 0, "max_wall_seconds": 30}
                budget.update(dict(scenario.get("budget_overrides") or {}))
                raw_contract = str(scenario.get("raw_contract") or "")
                task_pairs = list(task.items())
                budget_pairs = list(budget.items())
                if raw_contract == "duplicate-task":
                    task_pairs = [
                        ("task_id", task["task_id"]),
                        ("type", task["type"]),
                        ("problem_statement", "Return the first fixed Matrix reply."),
                        ("problem_statement", "Return the second fixed Matrix reply."),
                        ("repo_dir", task["repo_dir"]),
                        ("allow_network", False),
                    ]
                if raw_contract == "duplicate-budget":
                    budget_pairs = [
                        ("max_steps", 4),
                        ("max_steps", 5),
                        ("max_tokens", 0),
                        ("max_wall_seconds", 30),
                    ]
                if raw_contract in {"unique-task", "duplicate-task"}:
                    task_file.write_bytes(_matrix_json_object(task_pairs))
                else:
                    task_file.write_text(
                        json.dumps(task, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
                    )
                if raw_contract in {"unique-budget", "duplicate-budget"}:
                    budget_file.write_bytes(_matrix_json_object(budget_pairs))
                else:
                    budget_file.write_text(
                        json.dumps(budget, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
                    )
                values = {
                    "--task-file": str(task_file),
                    "--repo-dir": str(repo),
                    "--model-base-url": base_url,
                    "--model-name": "litellm_proxy/mock",
                    "--model-api-key": "sk-mock-test",
                    "--budget-file": str(budget_file),
                    "--out-dir": str(out),
                }
                values.update(dict(scenario.get("argv_overrides") or {}))
                argv = ["bash", str(launcher)]
                for _name, flag in _MATRIX_REQUIRED_FLAGS:
                    argv.extend((flag, str(values[flag])))
                cfg["mode"] = scenario.get("gateway_mode", case.get("gateway_mode", "fixed_reply"))
                cfg["reply_content"] = scenario.get("reply_content", case.get("reply_content", _MATRIX_REPLY))
                before_requests = len(cfg.get("requests") or [])
                cfg["rounds_base"] = before_requests
                observed = _matrix_run_process(
                    argv,
                    cwd=root,
                    env=_contract_clean_env(root, base_env, scenario.get("env")),
                    input_bytes=b"",
                    timeout=float(case.get("timeout") or 40),
                )
                row.update(observed)
                row["request_count"] = len(cfg.get("requests") or []) - before_requests
                actual_out = Path(str(values["--out-dir"]))
                if not actual_out.is_absolute():
                    actual_out = root / actual_out
                row["status_value"], row["status_error"] = _contract_read_status(actual_out / "status.json")
            except Exception as exc:
                row["start_error"] = f"SETUP_{type(exc).__name__.upper()}"
            finally:
                try:
                    shutil.rmtree(root)
                except OSError:
                    row["cleanup_complete"] = False
            rows.append(row)
    except Exception as exc:
        driver_error = f"DRIVER_{type(exc).__name__.upper()}"
    return {"std": True, "matrix_formal_rows": tuple(rows), "matrix_error": driver_error}


def _checked_case_result(case: dict, res: dict, reqs: list) -> dict:
    status = None
    try:
        outcome = case["check"](res, reqs)
        if isinstance(outcome, tuple) and len(outcome) == 3:
            ok, why, status = outcome
        else:
            ok, why = outcome
    except Exception as exc:
        ok, why = (False, f"runner error: {exc!r}")
        if case.get("formal_headless") or case.get("formal_status"):
            status = "error"
    row = {"id": case["id"], "ok": ok, "blocking": case.get("blocking", True), "why": why}
    if status is not None:
        row["status"] = status
    return row


CASES = [
    dict(
        id="headless_p_json_wellformed",
        output_format="json",
        blocking=False,
        reply_content="PUREJSON_8842 finished",
        check=chk_json_pure("PUREJSON_8842"),
    ),
    dict(
        id="headless_p_json_model_error_wellformed",
        output_format="json",
        gateway_mode="http_401",
        gate="headless_p_json_wellformed",
        check=chk_json_error_pure,
    ),
    dict(
        id="p34_ctl_task_text_reaches_wire",
        output_format="text",
        reply_content="ack",
        task="ATREFTOK_9934 acknowledge and stop.",
        probe_token="ATREFTOK_9934",
        check=chk_probe_present("task body"),
        timeout=90,
        blocking=False,
    ),
    dict(
        id="p34_ctl_bare_mention_not_auto_read",
        output_format="text",
        reply_content="ack",
        seed_files={"ctx_9934.md": "ATREFTOK_9934 the project ships on Tuesdays.\n"},
        probe_token="ATREFTOK_9934",
        task="There is a file called ctx_9934.md in this repo. Do not read it. Say ok and stop.",
        check=chk_probe_absent("content of a bare file-name mention"),
        timeout=90,
        blocking=False,
    ),
    dict(
        id="at_ref_expands_in_headless",
        output_format="text",
        reply_content="ack",
        seed_files={"ctx_9934.md": "ATREFTOK_9934 the project ships on Tuesdays.\n"},
        probe_token="ATREFTOK_9934",
        task="Summarize @ctx_9934.md in one line and stop.",
        gates=["p34_ctl_task_text_reaches_wire", "p34_ctl_bare_mention_not_auto_read"],
        check=chk_probe_present("content of the @-referenced file"),
        timeout=90,
    ),
    dict(
        id="budget_type_domain_control",
        formal_headless=True,
        formal_scenarios=_CONTRACT_BUDGET_VALID,
        timeout=45,
        blocking=False,
        check=chk_budget_type_domain_control,
    ),
    dict(
        id="budget_type_domain",
        formal_headless=True,
        formal_scenarios=(*_CONTRACT_BUDGET_VALID, *_CONTRACT_BUDGET_INVALID),
        timeout=45,
        gate="budget_type_domain_control",
        check=chk_budget_type_domain,
    ),
    dict(
        id="runtime_error_stdout_control",
        matrix_formal=True,
        matrix_formal_scenarios=({"label": "model-success", "gateway_mode": "seq_probe"},),
        gateway_mode="seq_probe",
        rounds=_MATRIX_FORMAL_ROUNDS,
        reply_content=_MATRIX_REPLY,
        timeout=40,
        formal_status=True,
        blocking=False,
        check=chk_runtime_error_stdout_control,
    ),
    dict(
        id="runtime_error_stdout",
        matrix_formal=True,
        matrix_formal_scenarios=(
            {
                "label": "model-runtime-error",
                "gateway_mode": "http_401",
                "env": {"MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT": "1"},
            },
        ),
        gateway_mode="seq_probe",
        rounds=_MATRIX_FORMAL_ROUNDS,
        reply_content=_MATRIX_REPLY,
        timeout=40,
        formal_status=True,
        gate="runtime_error_stdout_control",
        check=chk_runtime_error_stdout,
    ),
]


def _run_one(cli, env, agent_dir: Path, case: dict) -> dict:
    _cfg_over = dict(
        mode=case.get("gateway_mode", "fixed_reply"),
        reply_content=case.get("reply_content", "DONE_SENTINEL"),
        probe_token=case.get("probe_token") or None or "",
    )
    if case.get("rounds") is not None:
        _cfg_over["rounds"] = case["rounds"]
    cfg = MG.new_cfg(**_cfg_over)
    srv = MG.make_server(cfg, 0)
    if case.get("matrix_formal"):
        cfg["matrix_effect"] = True
        srv.RequestHandlerClass = _MatrixEffectHandler
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    if case.get("matrix_formal"):
        res = {"std": True, "matrix_formal_rows": (), "matrix_error": "DRIVER_DID_NOT_RETURN"}
        reqs = []
        try:
            res = _drive_matrix_formal(agent_dir, f"http://127.0.0.1:{port}", cfg, env, case)
            reqs = list(cfg["requests"])
        except Exception as exc:
            res["matrix_error"] = f"DRIVER_{type(exc).__name__.upper()}"
        finally:
            srv.shutdown()
            srv.server_close()
        return _checked_case_result(case, res, reqs)
    if case.get("formal_headless"):
        res = {"std": True, "formal_rows": (), "formal_error": "DRIVER_DID_NOT_RETURN"}
        reqs = []
        try:
            res = _drive_contract_formal(agent_dir, f"http://127.0.0.1:{port}", cfg, env, case)
            reqs = list(cfg["requests"])
        except Exception as exc:
            res["formal_error"] = f"DRIVER_{type(exc).__name__.upper()}"
        finally:
            srv.shutdown()
            srv.server_close()
        return _checked_case_result(case, res, reqs)
    repo = Path(tempfile.mkdtemp(prefix=f"headless_{case['id']}_"))
    res = {
        "std": True,
        "rc": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "repo": str(repo),
        "agent_dir": str(agent_dir),
    }
    reqs = []
    try:
        argv = list(cli) + [
            "-p",
            case.get("task", "Say the magic word and stop."),
            "--output-format",
            case.get("output_format", "text"),
        ]
        argv += list([])
        cenv = dict(env)
        cenv["HOME"] = str(repo)
        cenv["D2A_MODEL_BASE_URL"] = f"http://127.0.0.1:{port}"
        cenv["D2A_MODEL_NAME"] = "litellm_proxy/mock"
        cenv["D2A_MODEL_API_KEY"] = "sk-mock-test"
        if case.get("env"):
            cenv.update(case["env"])
        for _fn, _content in (case.get("seed_files") or {}).items():
            _sp = repo / _fn
            _sp.parent.mkdir(parents=True, exist_ok=True)
            _sp.write_text(_content)
        try:
            r = subprocess.run(
                argv,
                cwd=str(repo),
                env=cenv,
                capture_output=True,
                text=True,
                timeout=case.get("timeout", 90),
            )
            res["rc"], res["stdout"], res["stderr"] = (r.returncode, r.stdout or "", r.stderr or "")
        except subprocess.TimeoutExpired:
            res["timed_out"] = True
            res["rc"] = -1
        reqs = list(cfg["requests"])
    finally:
        srv.shutdown()
        srv.server_close()
    return _checked_case_result(case, res, reqs)


def _selected_cases(cases: list) -> list:
    """When `D2A_ONLY_IDS` (comma separated) is non-empty, run only those cases; unset = full run,
    bit-for-bit unchanged behaviour.

    Lets a single case be re-tested on its own without running the whole layer. A filtered run is
    NOT a complete score: the
    `D2A_ONLY_ACTIVE` line is the explicit marker for the merge tool; without it the result must
    not be merged as an increment.
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


def run_smoke(agent_dir: Path) -> list:
    cases = _selected_cases(CASES)
    print_cli, env = _discover_cli(agent_dir, require_print=True)
    management_cli, _ = _discover_cli(agent_dir, require_print=False)
    results = {}
    for c in cases:
        if c.get("matrix_formal"):
            results[c["id"]] = _run_one((), env, agent_dir, c)
            continue
        if c.get("formal_headless"):
            results[c["id"]] = _run_one((), env, agent_dir, c)
            continue
        is_management = False
        cli = management_cli if is_management else print_cli
        if cli is None:
            capability = "bare management CLI" if is_management else "-p/--print CLI"
            results[c["id"]] = {
                "id": c["id"],
                "ok": True,
                "blocking": c.get("blocking", True),
                "why": f"inconclusive: build exposes no discoverable {capability} -> skipped",
            }
            continue
        results[c["id"]] = _run_one(cli, env, agent_dir, c)
    return SC.apply_gates(cases, (results[c["id"]] for c in cases))


def _selftest() -> bool:
    """Checker selftest (proves discriminating power, needs no build): every mutant must FAIL, every
    correct fixture must PASS, and a broken observation must be reported as an error."""
    jf = chk_json_pure("XYZ")
    jclean = jf({"std": True, "rc": 0, "stdout": '{"exit_status":"Submitted","result":"XYZ ok"}\n'})
    jnoise = jf({"std": True, "rc": 0, "stdout": '[INFO] starting\n{"result":"XYZ"}\n'})
    jnan = jf({"std": True, "rc": 0, "stdout": '{"result":"XYZ","value":NaN}\n'})
    je = chk_json_error_pure
    jerr_clean = je(
        {"std": True, "rc": 1, "stdout": '{"exit_status":"Error","result":"model failed"}\n', "stderr": ""},
        [{"request": True}],
    )
    jerr_empty = je({"std": True, "rc": 1, "stdout": "", "stderr": "failed"}, [{"request": True}])

    def _contract_row(label, rc, status, requests, **extra):
        row = {
            "label": label,
            "rc": rc,
            "stdout": b"",
            "stderr": b"",
            "timed_out": False,
            "cleanup_complete": True,
            "start_error": "",
            "status_value": {"exit_status": status},
            "status_error": "",
            "request_count": requests,
        }
        row.update(extra)
        return row

    contract_valid = (_contract_row("valid-zero", 0, "Submitted", 1), _contract_row("valid-positive", 0, "Submitted", 1))
    contract_invalid = tuple(
        (
            _contract_row(label, 2, "Error", 0)
            for label in ("invalid-boolean", "invalid-string", "invalid-float", "invalid-negative")
        )
    )
    contract_control = chk_budget_type_domain_control({"formal_rows": contract_valid, "formal_error": ""})
    contract_pass = chk_budget_type_domain({"formal_rows": (*contract_valid, *contract_invalid), "formal_error": ""})
    contract_mutant_rows = list((*contract_valid, *contract_invalid))
    contract_mutant_rows[2] = _contract_row("invalid-boolean", 0, "Submitted", 1)
    contract_mutant = chk_budget_type_domain({"formal_rows": tuple(contract_mutant_rows), "formal_error": ""})
    contract_error_rows = [dict(row) for row in (*contract_valid, *contract_invalid)]
    contract_error_rows[-1]["status_error"] = "MISSING"
    contract_error_rows[-1]["status_value"] = None
    contract_error = chk_budget_type_domain({"formal_rows": tuple(contract_error_rows), "formal_error": ""})

    def _matrix_row(label, rc, requests, stdout, status, **extra):
        row = {
            "label": label,
            "rc": rc,
            "stdout": stdout,
            "stderr": b"",
            "timed_out": False,
            "cleanup_complete": True,
            "start_error": "",
            "status_value": None if status == "MISSING" else {"exit_status": status},
            "status_error": "MISSING" if status == "MISSING" else "",
            "request_count": requests,
        }
        row.update(extra)
        return row

    def _matrix_result(key, rows):
        return {key: tuple(rows), "matrix_error": ""}
    matrix_runtime_gate = chk_runtime_error_stdout_control(
        _matrix_result("matrix_formal_rows", (_matrix_row("model-success", 0, 2, _MATRIX_REPLY_BYTES, "Submitted"),))
    )
    matrix_runtime_row = _matrix_row("model-runtime-error", 1, 1, b"", "Error")
    matrix_runtime_pass = chk_runtime_error_stdout(_matrix_result("matrix_formal_rows", (matrix_runtime_row,)))
    matrix_runtime_mutant = chk_runtime_error_stdout(
        _matrix_result("matrix_formal_rows", (dict(matrix_runtime_row, stdout=b"[no final answer]\n"),))
    )
    matrix_runtime_error = chk_runtime_error_stdout(
        _matrix_result("matrix_formal_rows", (dict(matrix_runtime_row, start_error="SETUP_OSERROR"),))
    )
    oks = [
        jclean[0],
        not jnoise[0],
        not jnan[0],
        jerr_clean[0],
        not jerr_empty[0],
        contract_control[0] and contract_control[2] == "pass",
        contract_pass[0] and contract_pass[2] == "pass",
        not contract_mutant[0] and contract_mutant[2] == "fail",
        not contract_error[0] and contract_error[2] == "error",
        matrix_runtime_gate[0] and matrix_runtime_gate[2] == "pass",
        matrix_runtime_pass[0] and matrix_runtime_pass[2] == "pass",
        not matrix_runtime_mutant[0] and matrix_runtime_mutant[2] == "fail",
        not matrix_runtime_error[0] and matrix_runtime_error[2] == "error",
    ]
    print(
        "[selftest] json(clean/noise/nan/error/empty)=%s contract-budget(control/pass/mutant/error)=%s "
        "runtime-error(gate/pass/mutant/error)=%s -> %s"
        % (
            (jclean[0], jnoise[0], jnan[0], jerr_clean[0], jerr_empty[0]),
            (contract_control[2], contract_pass[2], contract_mutant[2], contract_error[2]),
            (matrix_runtime_gate[2], matrix_runtime_pass[2], matrix_runtime_mutant[2], matrix_runtime_error[2]),
            "OK" if all(oks) else "BROKEN",
        )
    )
    return all(oks)


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir", nargs="?", default=None)
    ap.add_argument("--emit-json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(0 if _selftest() else 1)
    if not args.agent_dir:
        ap.error("agent_dir is required unless --selftest is given")
    agent_dir = Path(args.agent_dir)
    if (agent_dir / "agent").is_dir():
        agent_dir = agent_dir / "agent"
    if not any(((agent_dir / c / "yuyao").is_dir() for c in ("src", "."))) and (
        not (agent_dir / "src").is_dir()
    ):
        print(f"[headless_smoke] agent src not found: {agent_dir}", file=sys.stderr)
        if args.emit_json:
            print(
                'HEADLESS_RESULTS=[{"id": "_headless_harness", "ok": false, "blocking": true, "why": "no agent src"}]'
            )
            print("HEADLESS_SMOKE_FAIL")
        sys.exit(2)
    print(f"[headless_smoke] agent: {agent_dir}  {len(CASES)} cases\n")
    results = run_smoke(agent_dir)
    npass = sum((r["ok"] for r in results))
    for r in results:
        print(f"[{('PASS' if r['ok'] else 'FAIL')}] {r['id']:28s} {r['why']}")
    blocking_ok = all((r["ok"] for r in results if r.get("blocking")))
    if args.emit_json:
        print("HEADLESS_RESULTS=" + json.dumps(results, ensure_ascii=False))
        print("HEADLESS_SMOKE_OK" if blocking_ok else "HEADLESS_SMOKE_FAIL")
    print(f"\n== headless -p output-purity determinism smoke {npass}/{len(results)} passed ==")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
