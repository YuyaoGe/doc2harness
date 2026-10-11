#!/usr/bin/env python3
"""Startup fail-soft / offline robustness deterministic probes (separate process, zero real model budget).

Why it exists: spec §0.1.2/§7 require the agent to fail soft under a bad config, a corrupt
convention file, a missing directory, or no network -- warn and skip, start anyway, never a bare
`Traceback`, never a silent `exit 1`. Typical breakages -- a trailing comma wedging the parser, a
UTF-8 BOM crashing the parse -- blow up at construction/startup time, have nothing to do with the
model, and are deterministically decidable.

Mechanism: under a controlled temporary `HOME` (the bad-config fixture is written to
`~/.yuyao/config.json`) and controlled env, a child process constructs one session through the
§0.1.2 standard programmatic interface `yuyao.agent_api.create_session()` -- no model, no turn --
and reports `(std, ok, tb)`:
  - std=False (build has no create_session): inconclusive, skipped (contract checks cover it);
  - ok=True and tb=False: started with tolerance (PASS);
  - tb=True (bare raise/traceback during construction): fail-soft violation (FAIL).

Zero false positives: the golden control (a valid config must start)
must PASS, and each bad-config case is judged against it, so FAIL only means "valid starts but
the bad config crashes". BOM / bad JSON / unknown key assert tolerant loading; the missing
directory case guards lazy startup without a model.

Usage: python startup_smoke.py <agent_dir> [--emit-json] | --selftest
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import smoke_contract as SC  # noqa: E402

RESULT_TAG = "STARTUP_RESULT="
_LAUNCHER_CHILD_RC = 73
_FAKE_OLD_ENTRY_RC = 86
_WRONG_ENTRY_RC = 87
_LAUNCHER_CHILD_TAG = "PROBE_LAUNCHER_CHILD_8B31"
_REQUIRES_PYTHON_RE = re.compile(
    r"(?m)^\s*requires-python\s*=\s*[\"']([^\"']+)[\"']"
)
_PYTHON_CONTROL_ENV = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONUSERBASE",
    "PYTHONWARNINGS",
    "PYTHONBREAKPOINT",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "YUYAO_PYTHON",
)

# Child: discover the standard create_session() contract, construct one session, catch any
# construction-time exception, and report (std, ok, tb).
_CHILD = '''
import os, sys, json, traceback
sys.path.insert(0, %(src)r)
# A flat layout (yuyao/ at the project root) must also be covered: §0.1.2 only requires that
# the package import, it does not prescribe a layout.
sys.path.insert(0, __import__("os").path.dirname(%(src)r))
import importlib as _il
def _find_std():
    # "The module crashed at import because it read a bad config" must stay distinguishable from
    # "the module does not exist at all": the first is precisely the fail-soft violation this layer
    # exists to catch, and treating it as absent would report inconclusive (i.e. a pass). So only a
    # genuinely absent module counts as "no interface"; every other exception is recorded as an
    # import-time crash.
    _imp_tb = None
    for _mn in ("yuyao.agent_api","yuyao.d2a_api","yuyao.harness_api","agent_api"):
        try:
            _m = _il.import_module(_mn)
        except ModuleNotFoundError as _e:
            _nm = getattr(_e, "name", None)
            if _nm and (_nm == _mn or _mn.startswith(_nm + ".")):
                continue                      # the module genuinely does not exist
            _imp_tb = traceback.format_exc()  # its own dependency is missing => import-time crash
            continue
        except Exception:
            _imp_tb = traceback.format_exc()  # raised during import => import-time crash
            continue
        _f = getattr(_m, "create_session", None)
        if callable(_f): return _f, None
    return None, _imp_tb
_f, _imp_tb = _find_std()
res = {"std": _f is not None, "ok": False, "tb": False, "import_tb": _imp_tb is not None}
if _imp_tb:
    sys.stderr.write(_imp_tb)
if _f is not None:
    try:
        _s = _f()
        res["ok"] = True
        try: _s.close()
        except Exception: pass
    except Exception:
        res["tb"] = True
        traceback.print_exc()
sys.stdout.write("\\n" + %(tag)r + json.dumps(res) + "\\n")
sys.stdout.flush()
'''


def _declared_python_floor(agent_dir: Path) -> tuple[int, int] | None:
    """Read the artifact's own lower Python bound; do not invent one."""
    path = agent_dir / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        match = _REQUIRES_PYTHON_RE.search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None
    if match is None:
        return None
    floors = [
        (int(major), int(minor))
        for major, minor in re.findall(
            r"(?:^|,)\s*>=\s*(\d+)\.(\d+)", match.group(1)
        )
    ]
    return max(floors) if floors else None


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)


def _marker_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        raw = path.read_bytes()
    except OSError:
        return ["<unreadable>"]
    if len(raw) > 4096:
        return ["<oversize>"]
    try:
        return raw.decode("utf-8").splitlines()
    except UnicodeError:
        return ["<non-utf8>"]


def _child_marker(path: Path) -> tuple[str, dict | None]:
    if not path.is_file():
        return "missing", None
    try:
        raw = path.read_bytes()
        if len(raw) > 16384:
            return "oversize", None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "invalid", None
    return ("exact", value) if isinstance(value, dict) else ("invalid", None)


def _launcher_child_source(
    marker: Path,
    expected_entry: str,
    expected_args: list[str],
) -> str:
    """Controlled replacement for entry.py: exact argv/env receipt + sentinel rc."""
    return f'''#!/usr/bin/env python3
import json
import os
import sys

_expected_args = {expected_args!r}
_payload = {{
    "tag": {_LAUNCHER_CHILD_TAG!r},
    "entry_realpath": os.path.realpath(__file__),
    "expected_entry": {expected_entry!r},
    "argv": sys.argv[1:],
    "argv_exact": sys.argv[1:] == _expected_args,
    "python": [sys.version_info.major, sys.version_info.minor],
    "pythonpath": os.environ.get("PYTHONPATH"),
    "pythonhome": os.environ.get("PYTHONHOME"),
    "sys_path": list(sys.path),
    "prefix": os.path.realpath(sys.prefix),
    "base_prefix": os.path.realpath(getattr(sys, "base_prefix", sys.prefix)),
}}
_data = json.dumps(_payload, sort_keys=True).encode("utf-8")
_fd = os.open({str(marker)!r}, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(_fd, _data)
finally:
    os.close(_fd)
raise SystemExit({_LAUNCHER_CHILD_RC})
'''


def _healthy_python_wrapper(
    *,
    real_python: str,
    expected_entry: str,
    entry_marker: Path,
    probe_marker: Path,
    wrong_marker: Path,
    env_receipt: Path,
) -> str:
    """PATH tripwire that delegates probes and the exact controlled entry."""
    values = {
        "@@REAL@@": shlex.quote(real_python),
        "@@ENTRY@@": shlex.quote(expected_entry),
        "@@ENTRY_MARKER@@": shlex.quote(str(entry_marker)),
        "@@PROBE_MARKER@@": shlex.quote(str(probe_marker)),
        "@@WRONG_MARKER@@": shlex.quote(str(wrong_marker)),
        "@@ENV_RECEIPT@@": shlex.quote(str(env_receipt)),
        "@@WRONG_RC@@": str(_WRONG_ENTRY_RC),
    }
    body = r'''#!/bin/bash
set -u
REAL_PYTHON=@@REAL@@
EXPECTED_ENTRY=@@ENTRY@@
ENTRY_MARKER=@@ENTRY_MARKER@@
PROBE_MARKER=@@PROBE_MARKER@@
WRONG_MARKER=@@WRONG_MARKER@@
ENV_RECEIPT=@@ENV_RECEIPT@@
args=("$@")
i=0
while (( i < ${#args[@]} )); do
    arg="${args[$i]}"
    case "$arg" in
        -E|-I|-s|-S|-B|-u|-O|-OO) ((i += 1)); continue ;;
        -X|-W) ((i += 2)); continue ;;
        -X*|-W*) ((i += 1)); continue ;;
        -V|--version)
            printf 'version\n' >> "$PROBE_MARKER"
            exec "$REAL_PYTHON" "$@"
            ;;
        -c)
            printf 'code\n' >> "$PROBE_MARKER"
            exec "$REAL_PYTHON" "$@"
            ;;
        --) ((i += 1)); break ;;
        -m)
            printf 'module\n' >> "$WRONG_MARKER"
            exit @@WRONG_RC@@
            ;;
        -*)
            printf 'option:%s\n' "$arg" >> "$WRONG_MARKER"
            exit @@WRONG_RC@@
            ;;
        *) break ;;
    esac
done
if (( i >= ${#args[@]} )); then
    printf 'missing-script\n' >> "$WRONG_MARKER"
    exit @@WRONG_RC@@
fi
target="${args[$i]}"
if [[ "$target" = /* ]]; then
    target_real="$(/usr/bin/readlink -f -- "$target" 2>/dev/null || true)"
else
    target_real="$(/usr/bin/readlink -f -- "$PWD/$target" 2>/dev/null || true)"
fi
if [[ "$target_real" != "$EXPECTED_ENTRY" ]]; then
    printf 'script:%s\n' "$target_real" >> "$WRONG_MARKER"
    exit @@WRONG_RC@@
fi
printf 'entry\n' >> "$ENTRY_MARKER"
{
    printf 'PYTHONPATH=%s\n' "${PYTHONPATH-<unset>}"
    printf 'PYTHONHOME=%s\n' "${PYTHONHOME-<unset>}"
} >> "$ENV_RECEIPT"
exec "$REAL_PYTHON" "$@"
'''
    for old, new in values.items():
        body = body.replace(old, new)
    return body


def _old_python_wrapper(
    *,
    fake_version: tuple[int, int],
    expected_entry: str,
    probe_marker: Path,
    entry_marker: Path,
    other_marker: Path,
) -> str:
    """Fake old python3: truthful version probe, poison if used for entry."""
    version = f"{fake_version[0]}.{fake_version[1]}.0"
    values = {
        "@@VERSION@@": shlex.quote(version),
        "@@ENTRY@@": shlex.quote(expected_entry),
        "@@PROBE_MARKER@@": shlex.quote(str(probe_marker)),
        "@@ENTRY_MARKER@@": shlex.quote(str(entry_marker)),
        "@@OTHER_MARKER@@": shlex.quote(str(other_marker)),
        "@@OLD_RC@@": str(_FAKE_OLD_ENTRY_RC),
    }
    body = r'''#!/bin/bash
set -u
FAKE_VERSION=@@VERSION@@
EXPECTED_ENTRY=@@ENTRY@@
PROBE_MARKER=@@PROBE_MARKER@@
ENTRY_MARKER=@@ENTRY_MARKER@@
OTHER_MARKER=@@OTHER_MARKER@@
args=("$@")
i=0
while (( i < ${#args[@]} )); do
    arg="${args[$i]}"
    case "$arg" in
        -E|-I|-s|-S|-B|-u|-O|-OO) ((i += 1)); continue ;;
        -X|-W) ((i += 2)); continue ;;
        -X*|-W*) ((i += 1)); continue ;;
        -V|--version)
            printf 'version\n' >> "$PROBE_MARKER"
            printf 'Python %s\n' "$FAKE_VERSION"
            exit 0
            ;;
        -c)
            code="${args[$((i + 1))]:-}"
            if [[ "$code" == *version* || "$code" == *VERSION* || "$code" == *hexversion* ]]; then
                printf 'version\n' >> "$PROBE_MARKER"
                if [[ "$code" == *print* && "$code" != *xit* && "$code" != *assert* ]]; then
                    printf '%s\n' "$FAKE_VERSION"
                    exit 0
                fi
                exit 1
            fi
            printf 'code\n' >> "$OTHER_MARKER"
            exit 88
            ;;
        --) ((i += 1)); break ;;
        -m)
            printf 'module\n' >> "$OTHER_MARKER"
            exit 88
            ;;
        -*) printf 'option:%s\n' "$arg" >> "$OTHER_MARKER"; exit 88 ;;
        *) break ;;
    esac
done
if (( i >= ${#args[@]} )); then
    printf 'missing-script\n' >> "$OTHER_MARKER"
    exit 88
fi
target="${args[$i]}"
if [[ "$target" = /* ]]; then
    target_real="$(/usr/bin/readlink -f -- "$target" 2>/dev/null || true)"
else
    target_real="$(/usr/bin/readlink -f -- "$PWD/$target" 2>/dev/null || true)"
fi
if [[ "$target_real" = "$EXPECTED_ENTRY" ]]; then
    printf 'entry\n' >> "$ENTRY_MARKER"
else
    printf 'script:%s\n' "$target_real" >> "$OTHER_MARKER"
fi
exit @@OLD_RC@@
'''
    for old, new in values.items():
        body = body.replace(old, new)
    return body


def _launcher_scenario(
    agent_dir: Path,
    root: Path,
    scenario: str,
    floor: tuple[int, int] | None,
    timeout: int,
) -> dict:
    """Run one copied launcher against controlled entry/PATH/environment tripwires."""
    fixture = root / "artifact"
    shutil.copytree(agent_dir, fixture, symlinks=True)
    launcher = fixture / "run_agent.sh"
    entry = fixture / "entry.py"
    if entry.exists() or entry.is_symlink():
        entry.unlink()
    entry.parent.mkdir(parents=True, exist_ok=True)

    obs = root / "obs"
    bin_dir = root / "bin"
    home = root / "home"
    tmp = root / "tmp"
    cwd = root / "caller-cwd"
    repo = root / "repo"
    out_dir = root / "out"
    for path in (obs, bin_dir, home, tmp, cwd, repo, out_dir):
        path.mkdir(parents=True, exist_ok=True)
    task = root / "task.json"
    budget = root / "budget.json"
    task.write_text(
        '{"task_id":"probe-launcher","problem_statement":"controlled startup"}\n',
        encoding="utf-8",
    )
    budget.write_text('{"max_steps":1}\n', encoding="utf-8")
    expected_args = [
        "--task-file", str(task),
        "--repo-dir", str(repo),
        "--model-base-url", "http://127.0.0.1:9",
        "--model-name", "probe-launcher-model",
        "--model-api-key", "probe-launcher-key",
        "--budget-file", str(budget),
        "--out-dir", str(out_dir),
    ]
    expected_entry = str(entry.resolve())
    child_path = obs / "child.json"
    entry.write_text(
        _launcher_child_source(child_path, expected_entry, expected_args),
        encoding="utf-8",
    )
    entry.chmod(0o600)

    healthy_entry = obs / "healthy-entry.log"
    healthy_probe = obs / "healthy-probe.log"
    healthy_wrong = obs / "healthy-wrong.log"
    healthy_env = obs / "healthy-env.log"
    old_probe = obs / "old-probe.log"
    old_entry = obs / "old-entry.log"
    old_other = obs / "old-other.log"
    ambient_marker = obs / "ambient-site.log"

    healthy = _healthy_python_wrapper(
        real_python=sys.executable,
        expected_entry=expected_entry,
        entry_marker=healthy_entry,
        probe_marker=healthy_probe,
        wrong_marker=healthy_wrong,
        env_receipt=healthy_env,
    )
    _write_executable(bin_dir / "python", healthy)
    if scenario == "old-python3":
        if floor is None:
            raise RuntimeError("old-python3 scenario requires a declared floor")
        fake_version = (
            (floor[0], floor[1] - 1)
            if floor[1] > 0
            else (max(0, floor[0] - 1), 99)
        )
        _write_executable(
            bin_dir / "python3",
            _old_python_wrapper(
                fake_version=fake_version,
                expected_entry=expected_entry,
                probe_marker=old_probe,
                entry_marker=old_entry,
                other_marker=old_other,
            ),
        )
    else:
        fake_version = None
        _write_executable(bin_dir / "python3", healthy)

    env = _startup_env(str(home), str(root))
    for key in _PYTHON_CONTROL_ENV:
        env.pop(key, None)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", os.defpath)

    poison_selftest = True
    poison_detail = ""
    poison_path = ""
    poison_home = ""
    if scenario == "ambient-path":
        poison_dir = root / "ambient-pythonpath"
        poison_dir.mkdir()
        poison_path = str(poison_dir)
        (poison_dir / "sitecustomize.py").write_text(
            "import os\n"
            f"_fd=os.open({str(ambient_marker)!r},"
            "os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)\n"
            "os.write(_fd,b'sitecustomize\\n')\n"
            "os.close(_fd)\n",
            encoding="utf-8",
        )
        env["PYTHONPATH"] = poison_path
        proof = subprocess.run(
            [sys.executable, "-c", "pass"],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        poison_selftest = (
            proof.returncode == 0
            and _marker_lines(ambient_marker) == ["sitecustomize"]
        )
        poison_detail = f"path-proof-rc={proof.returncode}"
        ambient_marker.unlink(missing_ok=True)
    elif scenario == "ambient-home":
        poison_home_path = root / "ambient-pythonhome"
        poison_home_path.mkdir()
        poison_home = str(poison_home_path)
        proof_marker = obs / "home-proof.log"
        env["PYTHONHOME"] = poison_home
        proof = subprocess.run(
            [
                sys.executable,
                "-c",
                f"open({str(proof_marker)!r},'w').write('ran')",
            ],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        poison_selftest = proof.returncode != 0 and not proof_marker.exists()
        poison_detail = f"home-proof-rc={proof.returncode}"

    row = {
        "scenario": scenario,
        "expected_entry": expected_entry,
        "expected_args": expected_args,
        "floor": list(floor) if floor else None,
        "fake_version": list(fake_version) if fake_version else None,
        "poison_selftest": poison_selftest,
        "poison_detail": poison_detail,
        "poison_path": poison_path,
        "poison_home": poison_home,
        "timed_out": False,
        "fixture_error": "",
    }
    if not poison_selftest:
        row["fixture_error"] = "AMBIENT_POISON_SELFTEST_FAILED"
    elif not launcher.is_file():
        row["fixture_error"] = "RUN_AGENT_MISSING_AFTER_COPY"
    else:
        try:
            proc = subprocess.run(
                ["/bin/bash", str(launcher), *expected_args],
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            row["rc"] = proc.returncode
            row["stdout"] = (proc.stdout or "")[-2000:]
            row["stderr"] = (proc.stderr or "")[-2000:]
        except subprocess.TimeoutExpired as exc:
            row["timed_out"] = True
            row["rc"] = None
            row["stdout"] = (
                exc.stdout.decode(errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )[-2000:]
            row["stderr"] = (
                exc.stderr.decode(errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )[-2000:]
        except OSError as exc:
            row["fixture_error"] = f"LAUNCH_OSERROR:{type(exc).__name__}"
            row["rc"] = None
    marker_state, child = _child_marker(child_path)
    row.update(
        {
            "child_marker_state": marker_state,
            "child": child,
            "healthy_entry": _marker_lines(healthy_entry),
            "healthy_probe": _marker_lines(healthy_probe),
            "healthy_wrong": _marker_lines(healthy_wrong),
            "healthy_env": _marker_lines(healthy_env),
            "old_probe": _marker_lines(old_probe),
            "old_entry": _marker_lines(old_entry),
            "old_other": _marker_lines(old_other),
            "ambient_marker": _marker_lines(ambient_marker),
        }
    )
    return row


def _run_launcher_probe(agent_dir: Path, case: dict) -> tuple[dict, str]:
    kind = str(case["launcher_probe"])
    floor = _declared_python_floor(agent_dir)
    res = {
        "driver_error": "",
        "cleanup_complete": True,
        "unobservable": "",
        "declared_floor": list(floor) if floor else None,
        "scenarios": {},
    }
    if not (agent_dir / "run_agent.sh").is_file() or not (agent_dir / "entry.py").is_file():
        res["unobservable"] = "artifact lacks run_agent.sh or entry.py"
        return res, ""
    names = {
        "control": ("healthy",),
        "python-floor": ("old-python3",) if floor else (),
        "ambient-python": ("ambient-path", "ambient-home"),
    }.get(kind)
    if names is None:
        res["driver_error"] = f"unknown launcher probe kind: {kind}"
        return res, ""
    root = Path(tempfile.mkdtemp(prefix="startup_launcher_"))
    try:
        for name in names:
            res["scenarios"][name] = _launcher_scenario(
                agent_dir,
                root / name,
                name,
                floor,
                int(case.get("timeout", 25)),
            )
    except Exception as exc:  # noqa: BLE001
        res["driver_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            shutil.rmtree(root)
        except OSError:
            res["cleanup_complete"] = False
    err = "\n".join(
        str(row.get("stderr") or "")
        for row in res["scenarios"].values()
        if row.get("stderr")
    )
    return res, err


def _launcher_probe_error(res: dict | None) -> str:
    if not isinstance(res, dict):
        return "launcher probe did not return an observation"
    if res.get("driver_error"):
        return f"launcher driver error: {res['driver_error']}"
    if not res.get("cleanup_complete"):
        return "launcher fixture cleanup incomplete"
    fixture_errors = [
        f"{name}:{row.get('fixture_error')}"
        for name, row in (res.get("scenarios") or {}).items()
        if row.get("fixture_error")
    ]
    return ", ".join(fixture_errors)


def _exact_launcher_entry(row: dict) -> bool:
    child = row.get("child")
    return bool(
        not row.get("timed_out")
        and row.get("rc") == _LAUNCHER_CHILD_RC
        and row.get("child_marker_state") == "exact"
        and isinstance(child, dict)
        and child.get("tag") == _LAUNCHER_CHILD_TAG
        and child.get("entry_realpath") == row.get("expected_entry")
        and child.get("expected_entry") == row.get("expected_entry")
        and child.get("argv_exact") is True
        and child.get("argv") == row.get("expected_args")
    )


def chk_launcher_health_control(res, _err):
    """Zero-weight control: PATH-selected launcher reaches exact entry and rc."""
    error = _launcher_probe_error(res)
    if error:
        return False, error, "error"
    if res.get("unobservable"):
        return True, f"inconclusive: {res['unobservable']}", "na"
    row = (res.get("scenarios") or {}).get("healthy")
    if not isinstance(row, dict):
        return False, "healthy launcher scenario missing", "error"
    exact = _exact_launcher_entry(row)
    path_seen = row.get("healthy_entry") == ["entry"]
    wrong = row.get("healthy_wrong") or []
    if exact and not path_seen:
        return (
            True,
            "inconclusive: the launcher reaches the exact entry but bypasses the PATH tripwire, "
            "so the interpreter selection strategy cannot be attributed",
            "na",
        )
    ok = exact and path_seen and not wrong
    return (
        ok,
        f"exact entry={exact} healthy interpreter on PATH={path_seen} wrong={wrong} "
        f"rc={row.get('rc')} (control must forward all seven arguments exactly and propagate the child rc)",
        "pass" if ok else "fail",
    )


def chk_launcher_python_minimum_fallback(res, _err):
    """Reject a fake old python3, then use the healthy fallback."""
    error = _launcher_probe_error(res)
    if error:
        return False, error, "error"
    if res.get("unobservable"):
        return True, f"inconclusive: {res['unobservable']}", "na"
    floor = res.get("declared_floor")
    if not (
        isinstance(floor, list)
        and len(floor) == 2
        and all(isinstance(value, int) for value in floor)
    ):
        return (
            True,
            "inconclusive: the artifact declares no parsable requires-python >=X.Y lower bound",
            "na",
        )
    row = (res.get("scenarios") or {}).get("old-python3")
    if not isinstance(row, dict):
        return False, "old-python3 scenario missing", "error"
    child = row.get("child") if isinstance(row.get("child"), dict) else {}
    child_version = child.get("python")
    old_probed = bool(row.get("old_probe")) and set(row["old_probe"]) == {"version"}
    old_never_entered = not row.get("old_entry")
    healthy_entered = row.get("healthy_entry") == ["entry"]
    floor_met = (
        isinstance(child_version, list)
        and len(child_version) == 2
        and tuple(child_version) >= tuple(floor)
    )
    exact = _exact_launcher_entry(row)
    ok = bool(
        old_probed
        and old_never_entered
        and healthy_entered
        and floor_met
        and exact
        and not row.get("healthy_wrong")
        and not row.get("old_other")
    )
    return (
        ok,
        f"declared floor={floor} fake version={row.get('fake_version')} version probed={old_probed} "
        f"fake old reached entry={not old_never_entered} healthy fallback={healthy_entered} "
        f"child version={child_version} exact entry/rc={exact}",
        "pass" if ok else "fail",
    )


def _path_is_within(value: object, root: str) -> bool:
    if not isinstance(value, str) or not root:
        return False
    try:
        return os.path.commonpath([os.path.realpath(value), os.path.realpath(root)]) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


def chk_launcher_ambient_python_isolated(res, _err):
    """PYTHONPATH and PYTHONHOME stimuli cannot affect entry startup."""
    error = _launcher_probe_error(res)
    if error:
        return False, error, "error"
    if res.get("unobservable"):
        return True, f"inconclusive: {res['unobservable']}", "na"
    rows = res.get("scenarios") or {}
    path_row = rows.get("ambient-path")
    home_row = rows.get("ambient-home")
    if not isinstance(path_row, dict) or not isinstance(home_row, dict):
        return False, "ambient path/home scenarios missing", "error"
    path_child = (
        path_row.get("child")
        if isinstance(path_row.get("child"), dict)
        else {}
    )
    home_child = (
        home_row.get("child")
        if isinstance(home_row.get("child"), dict)
        else {}
    )
    poison_path = str(path_row.get("poison_path") or "")
    path_absent = all(
        not _path_is_within(value, poison_path)
        for value in (path_child.get("sys_path") or [])
    )
    path_clean = (
        _exact_launcher_entry(path_row)
        and path_row.get("healthy_entry") == ["entry"]
        and not path_row.get("healthy_wrong")
        and not path_row.get("ambient_marker")
        and path_absent
    )
    poison_home = str(home_row.get("poison_home") or "")
    home_clean = (
        _exact_launcher_entry(home_row)
        and home_row.get("healthy_entry") == ["entry"]
        and not home_row.get("healthy_wrong")
        and not _path_is_within(home_child.get("prefix"), poison_home)
        and not _path_is_within(home_child.get("base_prefix"), poison_home)
    )
    proofs = bool(path_row.get("poison_selftest") and home_row.get("poison_selftest"))
    ok = proofs and path_clean and home_clean
    return (
        ok,
        f"poison selftest={proofs} PYTHONPATH:entry={_exact_launcher_entry(path_row)} "
        f"site-marker={path_row.get('ambient_marker')} sys.path isolated={path_absent}; "
        f"PYTHONHOME:entry={_exact_launcher_entry(home_row)} "
        f"prefix={home_child.get('prefix')!r} poison={poison_home!r}",
        "pass" if ok else "fail",
    )


def chk_starts_clean(res, err):
    if res is None:
        return False, f"the child process reported no result (crash or timeout) err={err[-160:]!r}"
    if res.get("import_tb"):
        # The module blew up at import time. This is not "the interface is missing" but the very
        # fail-soft violation this layer targets (typically: reading ~/.yuyao/config.json during
        # import and raising on a BOM or bad JSON).
        return False, ("crashed at import time: the module exists but raised while importing "
                       "(traceback on stderr). A bad config must warn and skip, never crash on import.")
    if not res.get("std"):
        return True, ("inconclusive: this build exposes no standard create_session interface -> skipped "
                      "(covered separately by contract validation)")
    ok = bool(res.get("ok")) and not res.get("tb")
    return ok, (f"fault-tolerant startup succeeded={res.get('ok')} construction-time traceback={res.get('tb')} "
                f"(a bad config or missing directory must warn and skip, never raise during construction)")


# ---- case table (standalone startup probes; config = content written to
# ~/.yuyao/config.json, None = write nothing) ----
CASES = [
    # Syntactically invalid JSON (trailing comma / bad schema) -> must fall back to defaults or
    # report clearly, never crash bare during construction.
    dict(id="startup_broken_config_file", config='{"yolo": true,, }', check=chk_starts_clean),
    # Valid JSON with a UTF-8 BOM -> the BOM must be stripped or tolerated, not crash the parse.
    dict(id="startup_bom_config", config='\ufeff{"yolo": true}', check=chk_starts_clean),

    # Probe - launcher exact-template control (zero weight).
    # The artifact's run_agent.sh is copied to a fresh root with entry.py replaced by a controlled
    # startup child. The healthy python tripwire on PATH must see the exact sibling entry.py, the
    # child must receive the seven arguments in order, write a unique JSON marker, and exit with
    # sentinel rc=73, which the launcher must pass through unchanged. A launcher using an absolute
    # interpreter that bypasses PATH only scores NA -- it cannot attribute the two PATH/env cases below.
    dict(id="launcher_health_control", launcher_probe="control",
         timeout=25, formal_status=True, blocking=False,
         check=chk_launcher_health_control),

    # launcher_python_minimum_fallback
    # Spec anchor: §0.1.1 - run_agent.sh is the root delivery entry point and carries the
    # seven-argument public calling surface.
    # Truth table:
    #   control: the previous case proves a healthy PATH interpreter closes the loop on exact
    #     entry/argv/marker/rc;
    #   target: read the artifact's own declared requires-python >=X.Y from pyproject.toml. First on
    #     PATH is `python3`, an executable but fake-old interpreter; `python` is the healthy fallback.
    #     The version probe must hit the fake-old one but must never enter entry; the healthy
    #     interpreter must then run the exact entry, satisfy the declared floor, and produce the exact
    #     child marker and rc=73;
    #   mutant: only does `command -v` / takes the first python3 by name, enters the fake-old entry
    #     directly and exits rc=86;
    #   NA: no parseable requires-python floor, or the control shows the launcher does not go through
    #     PATH -- neither is observable;
    #   ERROR: tripwire/poison self-proof, result protocol, or cleanup failure.
    dict(id="launcher_python_minimum_fallback", launcher_probe="python-floor",
         timeout=25, formal_status=True, gate="launcher_health_control",
         check=chk_launcher_python_minimum_fallback),

    # launcher_ambient_python_isolated
    # Spec anchor: §0.1.1 - run_agent.sh is the root delivery entry point and carries the
# seven-argument public calling surface.
    # Truth table:
    #   control: the same healthy launcher with exact entry/argv/marker/rc;
    #   target-A: a hostile PYTHONPATH whose sitecustomize first self-proves (under a bare healthy
    #     interpreter) that it does write a marker. Through the launcher the marker must be absent,
    #     the poisoned path must not reach the child's sys.path, and the child rc must be exactly 73;
    #   target-B: a PYTHONHOME with no stdlib first self-proves it blocks a bare interpreter. Through
    #     the launcher the child must still run under the healthy prefix and pass rc=73 through.
    #     Unsetting, replacing with a controlled value, or isolating with -E/-I are all accepted;
    #   mutant: prepends its own src but keeps the ambient PYTHONPATH, or inherits PYTHONHOME as-is;
    #   NA/ERROR: from an unobservable shared control, or fixture self-proof/protocol/cleanup failure.
    #     Never decided from stderr keywords.
    dict(id="launcher_ambient_python_isolated", launcher_probe="ambient-python",
         timeout=25, formal_status=True, gate="launcher_health_control",
         check=chk_launcher_ambient_python_isolated),
]


def _startup_env(home: str, workdir: str, extra: dict | None = None) -> dict[str, str]:
    """Build a deterministic child environment without inheriting host config."""
    allowed = ("PATH", "PYTHONPATH", "LD_LIBRARY_PATH", "VIRTUAL_ENV", "SYSTEMROOT")
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    env.update(
        {
            "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".config"),
            "TMPDIR": os.path.join(workdir, "tmp"),
            "MSWEA_SILENT_STARTUP": "1",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONHASHSEED": "0",
            "D2A_WORKER_ID": "0",
            "D2A_SEED": "20260817",
        }
    )
    env.update({str(key): str(value) for key, value in (extra or {}).items()})
    env.pop("YUYAO_HOME", None)
    env.pop("YUYAO_CONFIG", None)
    for key in list(env):
        if key.lower() in {
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        }:
            env.pop(key, None)
    return env


def _checked_case_result(case: dict, res: dict | None, err: str) -> dict:
    status = None
    try:
        outcome = case["check"](res, err)
        if isinstance(outcome, tuple) and len(outcome) == 3:
            ok, why, status = outcome
        else:
            ok, why = outcome
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"runner error: {exc!r}"
        if case.get("formal_status"):
            status = "error"
    row = {
        "id": case["id"],
        "ok": ok,
        "blocking": case.get("blocking", True),
        "why": why,
    }
    if status is not None:
        row["status"] = status
    return row


def _run_one(agent_dir: Path, case: dict) -> dict:
    if case.get("launcher_probe"):
        res, err = _run_launcher_probe(agent_dir, case)
        return _checked_case_result(case, res, err)
    # A TIMEOUT is retried once (in a fresh workdir) to shed load noise: false timeouts caused by
    # heavy concurrency, a stalled env, or litellm's slow import (~20s) usually complete on retry,
    # so healthy builds are not failed spuriously. A genuine hang times out both times and FAILs.
    # Retrying alone is not enough under load, so the default budget is 120s rather than 30s:
    # litellm's import alone costs ~20s, which puts a 30s budget right at the edge during batch
    # runs. A real startup hang stays hung and is caught by any budget, so the larger budget only
    # removes noise without losing discriminating power.
    code = _CHILD % {"src": str((agent_dir / "src").resolve()), "tag": RESULT_TAG}
    out = err = None
    timed_out = True
    for _attempt in range(2):
        workdir = tempfile.mkdtemp(prefix="startup_smoke_")
        tripwire_requests: list[dict] = []
        home = os.path.join(workdir, "home")
        os.makedirs(home, exist_ok=True)
        os.makedirs(os.path.join(workdir, "tmp"), exist_ok=True)
        cfg = case.get("config")
        if cfg is not None:
            cdir = os.path.join(home, ".yuyao")
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, "config.json"), "w", encoding="utf-8") as f:
                f.write(cfg)
        # home_files: seed plain FILES at arbitrary relative paths under HOME (`config=` can only
        # write config.json), e.g. a file where the build expects to mkdir a state directory:
        # `mkdir(exist_ok=True)` only tolerates an existing DIRECTORY. When omitted nothing runs.
        for rel, body in ({}).items():
            fp = os.path.join(home, rel)
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(body)
        extra_env = dict({})
        env = _startup_env(home, workdir, extra_env)
        try:
            r = subprocess.run([sys.executable, "-c", code], cwd=workdir, env=env,
                               capture_output=True, text=True, timeout=case.get("timeout", 120))
            out, err = (r.stdout or ""), (r.stderr or "")
            child_rc = r.returncode
            timed_out = False
            break
        except subprocess.TimeoutExpired:
            timed_out = True
            continue
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    if timed_out:
        return {"id": case["id"], "ok": False, "blocking": case.get("blocking", True),
                "why": "construction did not finish within the time limit (still timing out after a retry, "
                       "which suggests the startup path hangs)"}
    res = None
    for ln in out.splitlines():
        if ln.startswith(RESULT_TAG):
            try:
                res = json.loads(ln[len(RESULT_TAG):])
            except Exception:
                res = None
    if isinstance(res, dict):
        res["startup_request_count"] = len(tripwire_requests)
        res["child_rc"] = child_rc
    return _checked_case_result(case, res, err)


def _selected_cases(cases: list) -> list:
    """When `D2A_ONLY_IDS` (comma-separated) is non-empty, run only those cases; unset means run
    everything with bit-identical behaviour.

    Lets a single case be re-tested on its own without running the whole layer.
    A filtered run is NOT a complete score: the `D2A_ONLY_ACTIVE` line is the explicit marker for the
    merge tooling, and without it the results must not be merged as an increment.
    """
    only = {s.strip() for s in (os.environ.get("D2A_ONLY_IDS") or "").split(",") if s.strip()}
    if not only:
        return list(cases)
    picked = SC.dependency_closure(cases, only)
    need = {c["id"] for c in picked}
    print(f"D2A_ONLY_ACTIVE={len(picked)}/{len(cases)} ids={sorted(only)[:12]} "
          f"gates+={sorted(need - only)[:8]}", flush=True)
    return picked


def run_smoke(agent_dir: Path) -> list:
    from concurrent.futures import ThreadPoolExecutor
    _w = max(1, int(os.environ.get("D2A_STARTUP_WORKERS", "6") or "6"))
    cases = _selected_cases(CASES)
    results = {}
    with ThreadPoolExecutor(max_workers=_w) as ex:
        futs = {ex.submit(_run_one, agent_dir, c): c["id"] for c in cases}
        for fut in futs:
            r = fut.result()
            results[r["id"]] = r
    return SC.apply_gates(cases, (results[c["id"]] for c in cases))


def _write_synthetic_launcher(agent_dir: Path, mode: str) -> None:
    """Small offline launcher fixtures for the Probe truth table."""
    (agent_dir / "src" / "yuyao").mkdir(parents=True)
    (agent_dir / "entry.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    (agent_dir / "pyproject.toml").write_text(
        '[project]\nname="probe-fixture"\nversion="0"\n'
        'requires-python=">=3.10"\n',
        encoding="utf-8",
    )
    prelude = (
        "#!/bin/bash\n"
        "set -u\n"
        'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
    )
    checked = (
        'for candidate in python3 python; do\n'
        '  if command -v "$candidate" >/dev/null 2>&1 && '
        '"$candidate" -c \'import sys; '
        'raise SystemExit(0 if sys.version_info >= (3, 10) else 1)\' '
        '>/dev/null 2>&1; then\n'
        '    exec "$candidate" "$HERE/entry.py" "$@"\n'
        "  fi\n"
        "done\n"
        "exit 1\n"
    )
    if mode == "correct":
        body = prelude + "unset PYTHONPATH PYTHONHOME\n" + checked
    elif mode == "no-version":
        body = (
            prelude
            + "unset PYTHONPATH PYTHONHOME\n"
            + 'exec python3 "$HERE/entry.py" "$@"\n'
        )
    elif mode == "ambient-mutant":
        body = prelude + checked
    elif mode == "noop":
        body = prelude + f"exit {_LAUNCHER_CHILD_RC}\n"
    else:
        raise ValueError(mode)
    _write_executable(agent_dir / "run_agent.sh", body)


def _selftest() -> bool:
    """Offline truth table: correct, two target mutants, empty, NA, ERROR, gate."""
    by_id = {case["id"]: case for case in CASES}
    control = by_id["launcher_health_control"]
    version = by_id["launcher_python_minimum_fallback"]
    ambient = by_id["launcher_ambient_python_isolated"]
    root = Path(tempfile.mkdtemp(prefix="startup_probe_selftest_"))
    observed: dict[str, str] = {}
    try:
        fixtures = {}
        for mode in ("correct", "no-version", "ambient-mutant", "noop"):
            agent = root / mode
            agent.mkdir()
            _write_synthetic_launcher(agent, mode)
            fixtures[mode] = agent
        checks = [
            ("correct-control", fixtures["correct"], control, "pass"),
            ("correct-version", fixtures["correct"], version, "pass"),
            ("correct-ambient", fixtures["correct"], ambient, "pass"),
            ("version-control", fixtures["no-version"], control, "pass"),
            ("version-mutant", fixtures["no-version"], version, "fail"),
            ("ambient-control", fixtures["ambient-mutant"], control, "pass"),
            ("ambient-mutant", fixtures["ambient-mutant"], ambient, "fail"),
            ("empty-control", fixtures["noop"], control, "fail"),
        ]
        oks = []
        for label, agent, case, expected in checks:
            row = SC.finalize_case(_run_one(agent, case))
            observed[label] = row["status"]
            oks.append(row["status"] == expected)

        no_floor = chk_launcher_python_minimum_fallback(
            {
                "driver_error": "",
                "cleanup_complete": True,
                "unobservable": "",
                "declared_floor": None,
                "scenarios": {},
            },
            "",
        )
        driver_error = chk_launcher_ambient_python_isolated(
            {
                "driver_error": "synthetic",
                "cleanup_complete": True,
                "unobservable": "",
                "declared_floor": [3, 10],
                "scenarios": {},
            },
            "",
        )
        gated = SC.apply_gates(
            (control, version),
            (
                {
                    "id": control["id"],
                    "ok": False,
                    "blocking": False,
                    "status": "fail",
                    "why": "synthetic dead control",
                },
                {
                    "id": version["id"],
                    "ok": False,
                    "blocking": True,
                    "status": "fail",
                    "why": "must be gated",
                },
            ),
        )
        observed["no-floor"] = no_floor[2]
        observed["driver-error"] = driver_error[2]
        observed["failed-gate"] = gated[1]["status"]
        oks.extend(
            [
                no_floor[0] is True and no_floor[2] == "na",
                driver_error[0] is False and driver_error[2] == "error",
                gated[1]["status"] == "na",
            ]
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(
        "STARTUP_PROBE_SELFTEST="
        + json.dumps(
            {"statuses": observed, "result": "OK" if all(oks) else "BROKEN"},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return all(oks)


def _result_history(path: Path) -> dict[str, dict]:
    if not path.is_file():
        return {}
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("id"):
            rows[str(value["id"])] = value
    return rows


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir", nargs="?", default=None)
    ap.add_argument("--emit-json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--case-id", default="")
    ap.add_argument("--result-file", default="")
    args = ap.parse_args()
    if args.selftest:
        return 0 if _selftest() else 1
    if not args.agent_dir:
        ap.error("agent_dir is required unless --selftest is given")
    agent_dir = Path(args.agent_dir)
    if (agent_dir / "agent").is_dir():
        agent_dir = agent_dir / "agent"
    # Found if `yuyao/` sits under either `src/` or the project root: §0.1.2 does not mandate a
    # directory layout, so a flat layout is equally valid.
    if not any((agent_dir / c / "yuyao").is_dir() for c in ("src", ".")) \
            and not (agent_dir / "src").is_dir():
        print(f"[startup_smoke] agent src not found: {agent_dir}", file=sys.stderr)
        if args.emit_json:
            print('STARTUP_RESULTS=[{"id": "_startup_harness", "ok": false, "blocking": true, "why": "no agent src"}]')
            print("STARTUP_SMOKE_FAIL")
        sys.exit(2)
    print(f"[startup_smoke] agent: {agent_dir}  {len(CASES)} case(s)\n")
    cases = _selected_cases(CASES)
    if args.case_id:
        known = {case["id"] for case in CASES}
        if args.case_id not in known:
            print(f"STARTUP_SELECTION_ERROR=unknown --case-id: {args.case_id}", file=sys.stderr)
            return 2
        cases = [case for case in cases if case["id"] == args.case_id]
    results = []
    for case in cases:
        try:
            result = _run_one(agent_dir, case)
        except Exception as exc:  # noqa: BLE001
            result = {
                "id": case["id"],
                "ok": False,
                "blocking": case.get("blocking", True),
                "why": f"runner error: {exc!r}",
            }
            if case.get("formal_status"):
                result["status"] = "error"
        results.append(result)
    if args.case_id:
        closure = SC.dependency_closure(CASES, (args.case_id,))
        history = _result_history(Path(args.result_file)) if args.result_file else {}
        context = [
            history[case["id"]]
            for case in closure
            if case["id"] in history and case["id"] != args.case_id
        ]
        context.extend(results)
        gated = SC.apply_gates(closure, context)
        results = [row for row in gated if row["id"] == args.case_id]
    else:
        results = SC.apply_gates(cases, results)
    if args.result_file:
        path = Path(args.result_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for result in results:
                handle.write(
                    json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
    npass = sum(r["ok"] for r in results)
    for r in results:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['id']:30s} {r['why']}")
    blocking_ok = all(r["ok"] for r in results if r.get("blocking"))
    if args.emit_json:
        print("STARTUP_RESULTS=" + json.dumps(results, ensure_ascii=False))
        print("STARTUP_SMOKE_OK" if blocking_ok else "STARTUP_SMOKE_FAIL")
    print(f"\n== startup fail-soft determinism smoke: {npass}/{len(results)} passed ==")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
