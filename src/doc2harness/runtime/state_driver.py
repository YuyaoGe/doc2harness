"""Run one ``state`` command sequence in the artifact's interactive CLI."""

from __future__ import annotations
import argparse
import json
import os
import pty
import re
import select
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from doc2harness.runtime import mock_gateway
from doc2harness.runtime.direct_mock_driver import _digest, _trajectory
from doc2harness.runtime.resource_limits import container_memory_flags

_API_MARKER = "SBH_STATE_API_RESULT="


def _api_probe_source() -> str:
    """Return a small artifact-side probe for the public Agent API."""
    return '''\

import hashlib, importlib, json, os, signal, subprocess, sys, time, traceback
from pathlib import Path
sys.path.insert(0, "/sbh-artifact/src")
scenario_id = os.environ.get("SBH_SCENARIO_ID", "")
result = {
    "std": {"std": False}, "ok": False, "tb": False, "chat": "", "cwd": "",
    "returns": [], "messages": "", "messages_before": "", "roles": [],
    "roles_before": [], "mcp": [], "config": {}, "structured_facts": {},
}

def _text(value):
    return value if isinstance(value, str) else str(value)

def _surface(session):
    surface = {"std": True}
    for name in ("chat", "dispatch", "close", "config_get"):
        try:
            value = getattr(session, name)
            surface[name] = {"present": True, "type_ok": callable(value)}
        except Exception:
            surface[name] = {"present": False, "type_ok": False}
    try:
        value = getattr(session, "cwd")
        surface["cwd"] = {"present": True, "type_ok": isinstance(value, str)}
    except Exception:
        surface["cwd"] = {"present": False, "type_ok": False}
    try:
        value = getattr(session, "messages")
        surface["messages"] = {
            "present": True, "type_ok": isinstance(value, list)
            and all(isinstance(item, dict) for item in value),
        }
    except Exception:
        surface["messages"] = {"present": False, "type_ok": False}
    try:
        value = getattr(session, "mcp_servers")
        surface["mcp_servers"] = {
            "present": True, "type_ok": isinstance(value, list)
            and all(isinstance(item, str) for item in value),
        }
    except Exception:
        surface["mcp_servers"] = {"present": False, "type_ok": False}
    try:
        value = session.config_get("SBH_UNSET_CONFIGURATION_KEY_7731")
        surface["config_get"]["unset_returns"] = "None" if value is None else _text(value)
        surface["config_get"]["present_roundtrip"] = True
    except Exception:
        surface["config_get"]["unset_returns"] = "<raised>"
        surface["config_get"]["present_roundtrip"] = False
    return surface

def _snapshot_messages(session):
    messages = list(getattr(session, "messages", []) or [])
    roles = [[item.get("role")] for item in messages if isinstance(item, dict)]
    return json.dumps(messages, sort_keys=True), roles

def _call_public(session, name, *args):
    try:
        value = getattr(session, name)(*args)
        return {
            "ok": True,
            "return_type": type(value).__name__,
            "return_is_str": isinstance(value, str),
            "return": value[:8192] if isinstance(value, str) else repr(value)[:8192],
            "error": "",
        }
    except BaseException as exc:
        return {
            "ok": False,
            "return_type": "",
            "return_is_str": False,
            "return": "",
            "error": type(exc).__name__ + ": " + str(exc)[:300],
        }

def _alive(real_kill, pid):
    if not pid:
        return False
    try:
        real_kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def _read_int(path):
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None

def _fd_links(pid):
    links = {}
    try:
        entries = list((Path("/proc") / str(pid) / "fd").iterdir())
    except OSError:
        return links
    for entry in entries:
        try:
            links[entry.name] = os.readlink(entry)
        except OSError:
            pass
    return links

def _wait_for(path, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()

def _snapshot_file(path):
    """Capture a memory file byte-exactly for before/after comparison."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"present": False, "text": None, "sha256": None, "length": 0}
    return {
        "present": True,
        "text": data.decode("utf-8", "replace"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "length": len(data),
    }

def _public_call_facts(session, name, value):
    """Invoke a public method and record only its declared return shape."""
    method = getattr(session, name, None)
    if not callable(method):
        raise RuntimeError("PUBLIC_METHOD_MISSING_" + name.upper())
    returned = method(value)
    return {
        "return_type": type(returned).__name__,
        "return_is_str": isinstance(returned, str),
        "return_length": len(returned) if isinstance(returned, str) else None,
    }

def _child_directory_probe(create_session, variants, roots):
    """Observe whether a forked child inherits the parent's granted directory.

    Each declared variant runs in its own session.  The parent optionally grants
    an extra directory via ``/add-dir``, then either reads the three-file probe
    itself (``actor == "parent"``) or delegates to a child via ``/fork``.  The
    read results travel over the model wire, so the facts are recovered
    host-side from the recorded gateway requests; this probe only reports which
    session actions were driven and whether they returned text.

    ``/fork`` dispatches and returns immediately, so the probe waits for the
    child's own three tool results to reach the gateway ledger rather than
    sleeping a fixed interval.  The wait is bounded and reported.
    """
    rows = {}
    repo = Path(roots["repo"])
    extra = Path(roots["extra"])
    unauthorized = Path(roots["unauthorized"])
    for index, variant in enumerate(variants):
        label = str(variant.get("label") or index)
        for path in (repo, extra, unauthorized):
            path.mkdir(parents=True, exist_ok=True)
        (repo / "main.txt").write_text(variant["main_token"] + "\\n", encoding="utf-8")
        (extra / "extra.txt").write_text(variant["extra_token"] + "\\n", encoding="utf-8")
        (unauthorized / "outside.txt").write_text(
            variant["unauthorized_token"] + "\\n", encoding="utf-8"
        )
        session = create_session(mode="yolo", role="execute", cwd=str(repo))
        calls = []
        try:
            if variant.get("add_dir"):
                calls.append(_public_call_facts(session, "dispatch", "/add-dir " + str(extra)))
            prompt = variant["prompt"]
            if variant.get("actor") == "parent":
                calls.append(_public_call_facts(session, "chat", prompt))
            else:
                calls.append(_public_call_facts(session, "dispatch", "/fork " + prompt))
                # Bounded settle: the fork is fire-and-forget, so give the child
                # time to drive its reads without pinning a fixed wall-clock nap.
                deadline = time.monotonic() + float(variant.get("settle_s") or 2.0) * 4
                while time.monotonic() < deadline:
                    time.sleep(0.05)
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        rows[label] = {
            "state": "ok", "shape": variant.get("shape"),
            "actor": variant.get("actor"), "prompt": variant["prompt"],
            "add_dir": bool(variant.get("add_dir")), "calls": calls,
        }
    return rows

def _clipboard_copy_probe(create_session, variants):
    """Observe the exact bytes ``/copy`` hands to the clipboard helper.

    A fake ``pbcopy`` is injected as the only clipboard binary on PATH; it
    records its stdin verbatim so the observer compares bytes rather than a
    decoded string.  The interpreter is forced to a non-UTF-8 C locale first:
    the defect under test is a copy path that encodes using the ambient locale
    instead of UTF-8, which only shows up when the two disagree.
    """
    import locale

    rows = {}
    root = Path("/sbh-workspace")
    for index, variant in enumerate(variants):
        label = str(variant.get("label") or index)
        repo = root / ("clipboard_%d" % index)
        fake_bin = root / ("clipboard_bin_%d" % index)
        repo.mkdir(parents=True, exist_ok=True)
        fake_bin.mkdir(parents=True, exist_ok=True)
        capture = root / variant["capture"]
        meta = root / variant["meta"]
        helper = fake_bin / "pbcopy"
        helper.write_text(
            "#!" + sys.executable + "\\n"
            "import json, os, sys\\n"
            "data = sys.stdin.buffer.read()\\n"
            "open(os.environ['SBH_CLIPBOARD_CAPTURE'], 'wb').write(data)\\n"
            "open(os.environ['SBH_CLIPBOARD_META'], 'w', encoding='ascii').write("
            "json.dumps({'argv': sys.argv, 'bytes': len(data)}))\\n",
            encoding="utf-8",
        )
        helper.chmod(0o700)
        os.environ["PATH"] = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["SBH_CLIPBOARD_CAPTURE"] = str(capture)
        os.environ["SBH_CLIPBOARD_META"] = str(meta)
        try:
            locale.setlocale(locale.LC_ALL, "C")
        except locale.Error:
            pass
        session = create_session(mode="yolo", role="execute", cwd=str(repo))
        chat = _call_public(session, "chat", variant["prompt"])
        copied = _call_public(session, "dispatch", "/copy")
        closed = _call_public(session, "close")
        raw = capture.read_bytes() if capture.exists() else None
        try:
            helper_meta = json.loads(meta.read_text(encoding="ascii"))
        except (OSError, ValueError):
            helper_meta = None
        rows[label] = {
            "state": "ok",
            "locale": {
                "preferred": locale.getpreferredencoding(False),
                "filesystem": sys.getfilesystemencoding(),
                "utf8_mode": int(sys.flags.utf8_mode),
                "lc_all": os.environ.get("LC_ALL", ""),
                "lang": os.environ.get("LANG", ""),
            },
            "chat": chat, "dispatch": copied, "close": closed,
            "capture_present": raw is not None,
            "capture_hex": raw.hex() if raw is not None else None,
            "capture_length": len(raw) if raw is not None else None,
            "helper_meta": helper_meta,
        }
    return rows


def _memory_residency_probe(create_session, variants):
    """Measure how much memory a fixed top-k query keeps resident.

    Each variant seeds the same three ranked markers plus a decoy archive whose
    size differs by an order of magnitude.  A bounded top-k implementation
    should keep resident memory flat as the archive grows; loading the whole
    archive shows up as an RSS delta that scales with the entry count.

    Every variant runs in its own subprocess.  RSS is a whole-process figure, so
    a shared process would carry the previous variant's high-water mark into the
    next baseline and, worse, leave the sampler self-check unable to observe its
    own allocation once the heap is already large - the check would then read as
    "RSS is unobservable here" rather than as a real measurement.
    """
    rows = {}
    root = Path("/sbh-workspace")
    base_url = os.environ.get("D2A_MODEL_BASE_URL", "")

    def request_count():
        """Read the gateway's request count so each variant can be sliced out.

        Both variants send the same probe token, so the token alone cannot say
        which variant a request belongs to; the ledger boundary can.
        """
        try:
            import urllib.request
            root_url = base_url.rsplit("/v1", 1)[0]
            with urllib.request.urlopen(root_url + "/__d2a_seen?token=x", timeout=5) as response:
                return int(json.loads(response.read()).get("requests") or 0)
        except Exception:
            return None

    for index, variant in enumerate(variants):
        label = str(variant.get("label") or index)
        before = request_count()
        repo = root / ("memory_residency_%d" % index)
        (repo / "memory").mkdir(parents=True, exist_ok=True)
        markers = list(variant["markers"])
        (repo / "MEMORY.md").write_text(
            "# Memory\\n"
            "- " + markers[0] + " zirconium quasar heliotrope zirconium quasar heliotrope\\n"
            "- " + markers[1] + " zirconium quasar\\n"
            "- " + markers[2] + " zirconium\\n"
            "- " + variant["warm_query"] + " warm-up control\\n",
            encoding="utf-8",
        )
        with (repo / "memory" / "archive-2000-01-01.md").open("w", encoding="utf-8") as handle:
            handle.write("# Archived memory\\n")
            for entry in range(int(variant["entries"])):
                handle.write(
                    "- " + variant["decoy"] + ("%06d" % entry)
                    + " ordinary deployment preference number " + str(entry) + "\\n"
                )
        completed = subprocess.run(
            [sys.executable, "-c", os.environ["SBH_RESIDENCY_WORKER_SOURCE"],
             json.dumps({"repo": str(repo), "variant": variant})],
            capture_output=True, text=True, check=False, timeout=300,
        )
        parsed = None
        for line in (completed.stdout or "").splitlines():
            if line.startswith("SBH_RESIDENCY_ROW="):
                try:
                    parsed = json.loads(line[len("SBH_RESIDENCY_ROW="):])
                except ValueError:
                    parsed = None
        rows[label] = parsed if isinstance(parsed, dict) else {
            "state": "error", "entries": int(variant["entries"]),
            "stderr_tail": (completed.stderr or "")[-400:],
        }
        rows[label]["request_span"] = [before, request_count()]
    return rows

def _close_retry_probe(create_session, scenario_id):
    """Drive the public close lifecycle and return structured local facts."""
    root = Path("/sbh-workspace")
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    pid_path = root / "close_helper.pid"
    state_path = root / "close_helper.state"
    heartbeat_path = root / "close_helper.heartbeat"
    lease_path = root / "close_helper.lease"
    helper = repo / "close_helper.py"
    helper.write_text(
        "from pathlib import Path\\n"
        "import os, signal, time\\n"
        "pid = Path(os.environ['D2A_CLOSE_PID'])\\n"
        "state = Path(os.environ['D2A_CLOSE_STATE'])\\n"
        "heartbeat = Path(os.environ['D2A_CLOSE_HEARTBEAT'])\\n"
        "lease = open(os.environ['D2A_CLOSE_LEASE'], 'w', encoding='ascii')\\n"
        "lease.write('OWNED\\\\n'); lease.flush()\\n"
        "pid.write_text(str(os.getpid()), encoding='ascii')\\n"
        "state.write_text('START\\\\n', encoding='ascii')\\n"
        "def finish(signum, frame):\\n"
        "    with state.open('a', encoding='ascii') as fh:\\n"
        "        fh.write('SIGNAL:' + str(signum) + '\\\\n')\\n"
        "    raise SystemExit(0)\\n"
        "signal.signal(signal.SIGTERM, finish)\\n"
        "signal.signal(signal.SIGINT, finish)\\n"
        "counter = 0\\n"
        "print('CLOSE_HELPER_READY', flush=True)\\n"
        "while True:\\n"
        "    counter += 1\\n"
        "    heartbeat.write_text(str(counter), encoding='ascii')\\n"
        "    time.sleep(0.02)\\n",
        encoding="utf-8",
    )
    os.environ["D2A_CLOSE_PID"] = str(pid_path)
    os.environ["D2A_CLOSE_STATE"] = str(state_path)
    os.environ["D2A_CLOSE_HEARTBEAT"] = str(heartbeat_path)
    os.environ["D2A_CLOSE_LEASE"] = str(lease_path)
    target = scenario_id == "agent_api_close_retry_incomplete"
    session = create_session(mode="yolo", role="execute", cwd=str(repo))
    route = _call_public(session, "chat", "CLOSE_ROUTE_6501")
    if not _wait_for(pid_path):
        try:
            session.close()
        except BaseException:
            pass
        return {"ok": True, "std": {"std": False}, "structured_facts": {
            scenario_id: {"observed": False, "supported": False, "checks": ()}
        }, "close_route": route, "ctor": {"built": True, "cwd": str(repo)}}
    pid = _read_int(pid_path)
    real_kill = os.kill
    real_killpg = os.killpg
    real_getpgid = os.getpgid
    real_terminate = subprocess.Popen.terminate
    real_proc_kill = subprocess.Popen.kill
    real_wait = subprocess.Popen.wait
    try:
        target_pgid = real_getpgid(int(pid)) if pid else None
    except (OSError, TypeError, ValueError):
        target_pgid = None
    phase = {"name": "setup"}
    signal_ops = []

    def belongs(candidate_pid):
        try:
            return target_pgid is not None and real_getpgid(int(candidate_pid)) == target_pgid
        except (OSError, TypeError, ValueError):
            return int(candidate_pid or -1) == int(pid or -2)

    def faulted():
        return target and phase["name"] == "first"

    def note(op, target_pid, sig=None, will_fault=False):
        signal_ops.append({
            "phase": phase["name"], "op": op, "target": int(target_pid),
            "signal": int(sig) if sig is not None else None, "faulted": bool(will_fault),
        })

    def patched_killpg(pgid, sig):
        hit = target_pgid is not None and int(pgid) == int(target_pgid)
        reject = hit and faulted() and int(sig) != 0
        if hit:
            note("os.killpg", pgid, sig, reject)
        if reject:
            raise OSError("TRANSIENT_KILLPG")
        return real_killpg(pgid, sig)

    def patched_kill(candidate_pid, sig):
        hit = belongs(candidate_pid)
        reject = hit and faulted() and int(sig) != 0
        if hit and int(sig) != 0:
            note("os.kill", candidate_pid, sig, reject)
        if reject:
            raise OSError("TRANSIENT_KILL")
        return real_kill(candidate_pid, sig)

    def patched_terminate(proc):
        hit = belongs(proc.pid)
        reject = hit and faulted()
        if hit:
            note("Popen.terminate", proc.pid, signal.SIGTERM, reject)
        if reject:
            raise OSError("TRANSIENT_TERMINATE")
        return real_terminate(proc)

    def patched_proc_kill(proc):
        hit = belongs(proc.pid)
        reject = hit and faulted()
        if hit:
            note("Popen.kill", proc.pid, signal.SIGKILL, reject)
        if reject:
            raise OSError("TRANSIENT_PROC_KILL")
        return real_proc_kill(proc)

    def patched_wait(proc, timeout=None):
        hit = belongs(proc.pid)
        reject = hit and faulted()
        if hit:
            note("Popen.wait", proc.pid, None, reject)
        if reject:
            raise subprocess.TimeoutExpired("TRANSIENT_WAIT", timeout or 0)
        return real_wait(proc, timeout=timeout)

    def snapshot(label):
        child = _fd_links(pid)
        try:
            state_text = state_path.read_text(encoding="ascii")
        except OSError:
            state_text = ""
        lease_fds = {fd: link for fd, link in child.items() if link == str(lease_path)}
        return {
            "label": label, "pid_alive": _alive(real_kill, pid),
            "lease_fd_count": len(lease_fds), "lease_fds": lease_fds,
            "heartbeat": _read_int(heartbeat_path), "helper_state": state_text,
        }

    os.killpg = patched_killpg
    os.kill = patched_kill
    subprocess.Popen.terminate = patched_terminate
    subprocess.Popen.kill = patched_proc_kill
    subprocess.Popen.wait = patched_wait
    try:
        time.sleep(0.08)
        before = snapshot("before")
        phase["name"] = "first"
        first_close = _call_public(session, "close")
        phase["name"] = "observe-first"
        after_first_a = snapshot("after-first-a")
        time.sleep(0.16)
        after_first_b = snapshot("after-first-b")
        phase["name"] = "second"
        second_close = _call_public(session, "close")
        phase["name"] = "observe-second"
        time.sleep(0.12)
        after_second_a = snapshot("after-second-a")
        time.sleep(0.16)
        after_second_b = snapshot("after-second-b")
        phase["name"] = "third"
        ops_before_third = len(signal_ops)
        third_close = _call_public(session, "close")
        phase["name"] = "observe-third"
        time.sleep(0.08)
        after_third = snapshot("after-third")
        third_signal_ops = len(signal_ops) - ops_before_third
    finally:
        os.killpg = real_killpg
        os.kill = real_kill
        subprocess.Popen.terminate = real_terminate
        subprocess.Popen.kill = real_proc_kill
        subprocess.Popen.wait = real_wait
        try:
            session.close()
        except BaseException:
            pass
        if _alive(real_kill, pid):
            try:
                if target_pgid is not None:
                    real_killpg(target_pgid, signal.SIGKILL)
                else:
                    real_kill(pid, signal.SIGKILL)
            except OSError:
                pass
    base = {
        "public_chat": route.get("return_is_str") is True,
        "pid_started": before.get("pid_alive") is True,
        "fd_observed": int(before.get("lease_fd_count") or 0) == 1,
        "state_started": "START" in str(before.get("helper_state") or ""),
        "heartbeat_started": isinstance(before.get("heartbeat"), int),
    }
    first_ops = [row for row in signal_ops if row.get("phase") == "first"]
    second_ops = [row for row in signal_ops if row.get("phase") == "second"]
    if target:
        checks = {
            **base,
            "fault_delivered": bool(first_ops) and any(row.get("faulted") is True for row in first_ops),
            "first_pid_incomplete": after_first_a.get("pid_alive") is True and after_first_b.get("pid_alive") is True,
            "first_state_live": isinstance(after_first_a.get("heartbeat"), int) and isinstance(after_first_b.get("heartbeat"), int) and after_first_b["heartbeat"] > after_first_a["heartbeat"],
            "retry_attempted": bool(second_ops),
            "retry_pid_complete": after_second_a.get("pid_alive") is False and after_second_b.get("pid_alive") is False,
            "retry_fd_complete": int(after_second_b.get("lease_fd_count") or 0) == 0,
            "retry_state_quiescent": after_second_a.get("heartbeat") == after_second_b.get("heartbeat"),
            "third_idempotent": after_third.get("pid_alive") is False and int(after_third.get("lease_fd_count") or 0) == 0 and third_signal_ops == 0,
        }
    else:
        checks = {
            **base,
            "cleanup_attempted": bool(first_ops),
            "pid_closed": after_first_b.get("pid_alive") is False,
            "fd_closed": int(after_first_b.get("lease_fd_count") or 0) == 0,
            "heartbeat_stopped": after_first_b.get("heartbeat") == after_second_b.get("heartbeat"),
            "repeat_absorbed": not [row for row in signal_ops if row.get("phase") in {"second", "third"}],
            "close_returns": all(item.get("ok") is True for item in (first_close, second_close, third_close)),
        }
    row = {"observed": True, "supported": True, "checks": tuple(bool(item) for item in checks.values())}
    return {
        "ok": True, "std": {"std": True}, "structured_facts": {scenario_id: row},
        "close_route": route, "close_checks": checks,
        "ctor": {"built": True, "raised": "", "cwd": str(repo)},
    }

try:
    module = importlib.import_module("yuyao.agent_api")
    create_session = getattr(module, "create_session", None)
    if not callable(create_session):
        raise RuntimeError("create_session is unavailable")
    if scenario_id in {"agent_api_close_retry_incomplete_control", "agent_api_close_retry_incomplete"}:
        result.update(_close_retry_probe(create_session, scenario_id))
        print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
        raise SystemExit(0)
    elif scenario_id in {"child_add_dir_inheritance", "child_add_dir_inheritance_control"}:
        # Directory grants and the fork are driven here; the read results are
        # recovered host-side from the gateway ledger because they travel over
        # the model wire, not through the filesystem.
        plan = json.loads(os.environ.get("SBH_CHILD_DIRECTORY_PLAN", "{}"))
        result["child_directory_rows"] = _child_directory_probe(
            create_session, plan.get("variants") or (), plan.get("roots") or {}
        )
        result["ok"] = True
        result["std"] = {"std": True}
        print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
        raise SystemExit(0)
    elif scenario_id in {"copy_utf8_clipboard_bytes", "copy_utf8_clipboard_control"}:
        plan = json.loads(os.environ.get("SBH_CLIPBOARD_PLAN", "{}"))
        result["clipboard_rows"] = _clipboard_copy_probe(
            create_session, plan.get("variants") or ()
        )
        result["ok"] = True
        result["std"] = {"std": True}
        print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
        raise SystemExit(0)
    elif scenario_id in {
        "memory_query_topk_residency", "memory_query_topk_residency_control",
    }:
        plan = json.loads(os.environ.get("SBH_MEMORY_RESIDENCY_PLAN", "{}"))
        result["memory_residency_rows"] = _memory_residency_probe(
            create_session, plan.get("variants") or ()
        )
        result["ok"] = True
        result["std"] = {"std": True}
        print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
        raise SystemExit(0)
    elif os.environ.get("SBH_HELP_PROBE"):
        # Dispatch /help, read the commands it *claims* to support, then
        # dispatch each one once.  The claim source is the return value and
        # the console together: some builds print /help instead of returning
        # it, and reading one channel scores such a build as having no /help.
        #
        # The colon in the pattern is command syntax, not punctuation: one
        # build spells its command /flow:<name>, where /flow: is recognised
        # and a bare /flow is not, so stripping it invents a failure there.
        import io
        import re
        from contextlib import redirect_stdout
        session = create_session(mode="yolo", role="execute", cwd="/sbh-workspace")
        printed = io.StringIO()
        with redirect_stdout(printed):
            # _call_public truncates its return at 500 characters, which is right
            # for a status string but not here: the claim set is the whole command
            # table, and one build lists sixty commands.  Dispatch directly and
            # keep the full text, or the probe silently covers only the first few
            # and reports a clean result for a drift it never looked at.
            try:
                help_return = session.dispatch("/help")
            except BaseException as exc:
                help_return = ""
                result["help_error"] = type(exc).__name__ + ": " + str(exc)[:200]
        help_text = str(help_return or "") + " || " + printed.getvalue()
        result["returns"] = [str(help_return or "")]
        result["console"] = help_text
        claimed = sorted(set(re.findall(r"/[a-z][a-z0-9:-]{1,}", help_text)))
        skip = set(("/exit", "/quit", "/clear", "/reset", "/new"))
        # Placeholders that appear in prose ("Prefixes: /command @path") are
        # not command names; dispatching them would report a failure for a
        # line that was only documenting syntax.
        skip |= set(("/command", "/cmd", "/name", "/path", "/file", "/text",
              "/prompt", "/query", "/instruction", "/condition",
              "/level", "/args", "/arg", "/value", "/key", "/dir",
              "/url", "/n", "/x"))
        probe = dict()
        sink = io.StringIO()
        with redirect_stdout(sink):
            for name in [item for item in claimed if item not in skip][:60]:
                outcome = _call_public(session, "dispatch", name)
                if outcome.get("ok") is True:
                    probe[name] = str(outcome.get("return") or "")[:200]
                else:
                    probe[name] = str(outcome.get("error") or "")[:200]
        result["help_probe"] = probe
        result["help_claimed"] = claimed
        result["ok"] = True
        result["std"] = dict(std=True)
        print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
        raise SystemExit(0)
    elif os.environ.get("SBH_SESSION_FACT_COMMANDS"):
        # Drive the declared commands against a live session object and read the
        # facts that only that object carries: the message list, the role
        # sequence, and the registered MCP servers.  A terminal screen shows
        # none of them.
        #
        # Declared pre-messages are appended to the session's own list rather
        # than chatted, because the scenario states them as an existing
        # conversation the command then operates on - chatting them would let
        # the product append its own turns in between and would spend a model
        # round-trip per message.  The pre-command snapshot is what lets a judge
        # tell "the product dropped the history" apart from "this build never
        # exposed the history", which is why the stage1 smoke layer captures it too.
        commands = json.loads(os.environ["SBH_SESSION_FACT_COMMANDS"])
        premsgs = json.loads(os.environ.get("SBH_SESSION_PREMSGS", "[]"))
        session = create_session(mode="yolo", role="execute", cwd="/sbh-workspace")
        returns = []
        try:
            if premsgs:
                conversation = getattr(session, "messages", None)
                if isinstance(conversation, list):
                    try:
                        conversation.extend(premsgs)
                    except Exception:
                        pass
            before_text, before_roles = _snapshot_messages(session)
            result["messages_before"], result["roles_before"] = before_text, before_roles
            for item in commands:
                text = str(item)
                if text.startswith("/"):
                    returns.append(_call_public(session, "dispatch", text))
                else:
                    outcome = _call_public(session, "chat", text)
                    if outcome.get("ok") is not True:
                        outcome = _call_public(session, "dispatch", text)
                    returns.append(outcome)
            after_text, after_roles = _snapshot_messages(session)
            result["messages"], result["roles"] = after_text, after_roles
            result["message_count"] = len(list(getattr(session, "messages", []) or []))
            mcp_servers = getattr(session, "mcp_servers", None)
            if isinstance(mcp_servers, (list, tuple)):
                names = []
                for entry in mcp_servers:
                    if isinstance(entry, str):
                        names.append(entry)
                    elif isinstance(entry, dict):
                        names.append(str(entry.get("name") or entry.get("id") or entry.get("server") or entry))
                    else:
                        names.append(str(getattr(entry, "name", None) or getattr(entry, "id", None) or entry))
                result["mcp"] = names
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        result["returns"] = [str(item.get("return") or "") for item in returns]
        result["ok"] = True
        result["std"] = {"std": True}
        print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
        raise SystemExit(0)
    elif os.environ.get("SBH_WIRE_RETENTION_COMMANDS"):
        # Drive the declared commands through the public API, dispatching slash
        # commands and chatting plain text, exactly as the stage1 smoke layer does.
        # The interactive CLI runs a full agent loop per plain-text line, so on
        # some artifacts the first command never terminates its turn and the
        # later commands' requests never reach the wire.
        commands = json.loads(os.environ["SBH_WIRE_RETENTION_COMMANDS"])
        # A point about listing the rules in force has to start with them in
        # force.  The stage1 smoke layer passes the declared deny set to the session
        # factory, so a scenario that declares one must too, or the listing is
        # correct and empty and the point reads as a product that lost them.
        deny = json.loads(os.environ.get("SBH_SESSION_DENY", "[]"))
        session = create_session(
            mode="yolo", role="execute", cwd="/sbh-workspace",
            **({"deny": [str(rule) for rule in deny]} if deny else {}),
        )
        returns = []
        try:
            for item in commands:
                text = str(item)
                if text.startswith("/"):
                    returns.append(_call_public(session, "dispatch", text))
                else:
                    outcome = _call_public(session, "chat", text)
                    if outcome.get("ok") is not True:
                        # Some builds route plain text through dispatch instead.
                        outcome = _call_public(session, "dispatch", text)
                    returns.append(outcome)
            # Points about cwd persistence read the live session cwd after the
            # declared session ops; the terminal path could only scrape it from
            # the screen, which a build that does not re-print its prompt leaves
            # blank.  config_get is the contract read path for config-liveness
            # points, so a value a /config set|unset left in the live session is
            # reported the same way for every product rather than parsed out of a
            # /config listing.  Both are captured before close, while the session
            # still holds them.
            result["cwd"] = _text(getattr(session, "cwd", ""))
            config_get = getattr(session, "config_get", None)
            if callable(config_get):
                keys = []
                for command in commands:
                    parts = str(command).split()
                    if (
                        len(parts) >= 3 and parts[0] == "/config"
                        and parts[1] in ("set", "unset") and parts[2] not in keys
                    ):
                        keys.append(parts[2])
                if keys:
                    observed = {}
                    for key in keys:
                        try:
                            observed[key] = _text(config_get(key))
                        except Exception:
                            observed[key] = "<raised>"
                    result["config"] = observed
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        result["wire_retention_returns"] = returns
        # Judges that index ``returns`` positionally expect one entry per
        # declared command, exactly as the stage1 smoke layer records from each
        # dispatch call.  Console lines cannot be split back into that shape.
        # Some points look for their sentinel in the conversation as well as in
        # the command return - the stage1 smoke checker accepts any of return,
        # console or session messages - so the message text is published here
        # too rather than only on the session-fact path.
        # The plan point reads the names written under .plans, listed from the
        # session's working directory.
        try:
            result["plans"] = sorted(os.listdir(os.path.join('/sbh-workspace', '.plans')))
        except OSError:
            result["plans"] = None
        after_text, after_roles = _snapshot_messages(session)
        result["messages"], result["roles"] = after_text, after_roles
        # A judge that indexes returns counts only the declared commands.
        # Pre-messages are conversation state the command then operates on,
        # and the stage1 smoke layer records one entry per dispatched command -
        # counting them shifts every index and the judge reads the wrong
        # command's output.
        skip = int(os.environ.get("SBH_WIRE_PREMSG_COUNT") or 0)
        result["returns"] = [str(item.get("return") or "") for item in returns[skip:]]
        # A command can report its refusal by *raising* instead of returning
        # text, and that is the stronger of the two shapes: the caller cannot
        # mistake it for output.  Its message lives only on the call record, so
        # publishing returns alone drops it, and a point asking "is an unknown
        # command reported rather than swallowed" then reads an empty return
        # from a product that said "unknown command: /x (try /help)" out loud.
        # The stage1 smoke layer keeps exactly this channel - one ``CMD_ERR`` line per
        # raising command - and every judge still reads that field.
        errors = [
            "CMD_ERR " + repr(str(command)) + ": " + str(item.get("error") or "")
            for command, item in zip(commands[skip:], returns[skip:])
            if item.get("ok") is not True and item.get("error")
        ]
        if errors:
            result["error"] = "\\n".join(errors) + "\\n"
        result["ok"] = True
        result["std"] = {"std": True}
        print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
        raise SystemExit(0)
    kwargs = {}
    kwargs["cwd"] = "/sbh-workspace"
    session = create_session(**kwargs)
    result["ok"] = True
    result["std"] = _surface(session)
    result["cwd"] = str(getattr(session, "cwd", ""))
    result["ctor"] = {"built": True, "raised": "", "cwd": result["cwd"]}
    before_text, before_roles = _snapshot_messages(session)
    result["messages_before"], result["roles_before"] = before_text, before_roles
    if not scenario_id.startswith("agent_api_"):
        # Preserve the original model-turn probe for ENV_MODEL_VAR scenarios.
        result["chat"] = str(session.chat("hello there"))
    after_text, after_roles = _snapshot_messages(session)
    result["messages"], result["roles"] = after_text, after_roles
    if result["chat"]:
        result["returns"] = [result["chat"]]
    session.close()
except Exception:
    result["tb"] = True
    result["std"] = {"std": False}
    traceback.print_exc()
print("SBH_STATE_API_RESULT=" + json.dumps(result, sort_keys=True), flush=True)
'''


_MEMORY_RESIDENCY_WORKER = '''\

import gc, json, os, sys, threading, time
sys.path.insert(0, "/sbh-artifact/src")

payload = json.loads(sys.argv[1])
repo = payload["repo"]
variant = payload["variant"]

def rss_bytes():
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as handle:
            fields = handle.read().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, IndexError, ValueError):
        return None

def sampled_call(fn):
    samples = []
    stop = threading.Event()
    ready = threading.Event()

    def sample():
        ready.set()
        while not stop.is_set():
            value = rss_bytes()
            if value is not None:
                samples.append(value)
            stop.wait(0.001)

    baseline = rss_bytes()
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    ready.wait(1)
    returned = fn()
    time.sleep(0.02)
    stop.set()
    thread.join(timeout=1)
    after = rss_bytes()
    observed = [value for value in [baseline, after, *samples] if value is not None]
    peak = max(observed) if observed else None
    delta = None if baseline is None or peak is None else max(0, peak - baseline)
    return returned, {
        "baseline": baseline, "peak": peak, "after": after,
        "delta": delta, "samples": len(samples),
    }

def sampler_probe(size):
    holder = []

    def allocate():
        block = bytearray(size)
        for index in range(0, len(block), 4096):
            block[index] = 1
        holder.append(block)
        time.sleep(0.04)
        return len(block)

    allocated, observed = sampled_call(allocate)
    holder.clear()
    gc.collect()
    observed["allocated"] = allocated
    return observed

def call_public(session, name, *args):
    try:
        value = getattr(session, name)(*args)
        return {
            "ok": True, "return_type": type(value).__name__,
            "return_is_str": isinstance(value, str),
            "return": value[:8192] if isinstance(value, str) else repr(value)[:8192],
            "error": "",
        }
    except BaseException as exc:
        return {
            "ok": False, "return_type": "", "return_is_str": False, "return": "",
            "error": type(exc).__name__ + ": " + str(exc)[:300],
        }

row = {"state": "error", "entries": int(variant["entries"])}
try:
    from yuyao.agent_api import create_session
    session = create_session(mode="yolo", role="execute", cwd=repo)
    index_call = call_public(session, "dispatch", "/memory status")
    gc.collect()
    time.sleep(0.05)
    measured, rss = sampled_call(
        lambda: call_public(session, "chat", variant["target_prompt"])
    )
    # The self-check runs before close so it shares the measured heap state.
    sampler = sampler_probe(int(variant["rss_probe_bytes"]))
    closed = call_public(session, "close")
    row = {
        "state": "ok", "entries": int(variant["entries"]),
        "index_call": index_call, "measured": measured,
        "rss": rss, "rss_sampler_probe": sampler, "close": closed,
    }
except BaseException as exc:
    row["error"] = type(exc).__name__ + ": " + str(exc)[:300]
print("SBH_RESIDENCY_ROW=" + json.dumps(row, sort_keys=True), flush=True)
'''


def _calls_return_text(row: dict) -> bool:
    calls = row.get("calls") or []
    return bool(calls) and all((item.get("return_is_str") is True for item in calls))


_CHILD_DIRECTORY_SCENARIOS = frozenset({"child_add_dir_inheritance", "child_add_dir_inheritance_control"})
_CHILD_DIRECTORY_RECIPE_KEY = "api_probe_variants"
_CHILD_DIRECTORY_ROOTS = (
    ("repo", "__REPO__", "repo"),
    ("extra", "__EXTRA__", "extra"),
    ("unauthorized", "__UNAUTHORIZED__", "unauthorized"),
)
_CHILD_DIRECTORY_WORKSPACE = "/sbh-workspace"


def _child_directory_roots() -> dict[str, str]:
    """Return the absolute container path for each declared probe root."""
    return {
        role: f"{_CHILD_DIRECTORY_WORKSPACE}/{directory}"
        for role, _placeholder, directory in _CHILD_DIRECTORY_ROOTS
    }


def _resolve_child_directory_stub(stub: object, variant_count: int = 1) -> object:
    """Substitute the declared root placeholders and repeat the declared rounds.

    The recipe declares one variant's worth of rounds because the stage1 smoke
    layer forks a fresh process per variant and rebases the gateway's round
    counter each time.  This driver walks the variants inside a single probe process, so
    the round sequence is repeated once per variant instead; a single shared
    sequence would leave the first variant to consume every round and every
    later variant to receive ``phase-done`` on its first request.
    """
    roots = _child_directory_roots()
    encoded = json.dumps(stub)
    for role, placeholder, _directory in _CHILD_DIRECTORY_ROOTS:
        encoded = encoded.replace(placeholder, roots[role])
    resolved = json.loads(encoded)
    rounds = resolved.get("rounds")
    if isinstance(rounds, list) and variant_count > 1:
        resolved["rounds"] = [
            json.loads(json.dumps(entry)) for _index in range(variant_count) for entry in rounds
        ]
    return resolved


def _child_directory_plan(
    scenario_id: str, execution_parameters: tuple[tuple[str, object], ...]
) -> dict[str, object] | None:
    """Build the child-inheritance plan from the recipe's declared variants."""
    if scenario_id not in _CHILD_DIRECTORY_SCENARIOS:
        return None
    declared = dict(execution_parameters).get(_CHILD_DIRECTORY_RECIPE_KEY)
    if not declared:
        return None
    variants = [dict(_unfreeze(variant)) for variant in declared]
    required = ("prompt", "main_token", "extra_token", "unauthorized_token")
    if any((key not in variant for variant in variants for key in required)):
        return None
    return {"variants": variants, "roots": _child_directory_roots()}


def _child_directory_hits(requests: list[dict], prompt: str, markers: dict) -> dict | None:
    """Recover one variant's read results from the recorded gateway requests.

    The child's reads reach the gateway as tool results on the request that
    carries the variant's probe token.  The request with the most results is the
    completed three-file round; fewer results means the round never finished.
    """
    candidates = [
        request
        for request in requests
        if (request.get("probes") or {}).get(prompt) and isinstance(request.get("tool_result_texts"), list)
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda request: len(request["tool_result_texts"]))
    texts = selected.get("tool_result_texts") or []
    blob = "\n".join((str(text) for text in texts))
    return {
        "result_count": len(texts),
        "main": markers["main"] in blob,
        "extra": markers["extra"] in blob,
        "unauthorized": markers["unauthorized"] in blob,
    }


def _child_directory_checks(scenario_id: str, rows: dict, requests: list[dict]) -> dict[str, bool] | None:
    """Derive the child-inheritance checks from the recorded wire evidence.

    The control only asserts liveness: the parent's own ``/add-dir`` grant is
    real, and a fresh child can at least read the main tree.  The negative
    boundary - a child must not reach an ungranted sibling - belongs to the
    target alone.
    """
    if scenario_id.endswith("_control"):
        expected = {"parent-grant", "child-empty"}
    else:
        expected = {"child-target", "child-empty-target"}
    if set(rows) != expected:
        return None
    hits = {}
    for label in expected:
        row = rows[label]
        markers = {
            "main": row.get("main_token") or "",
            "extra": row.get("extra_token") or "",
            "unauthorized": row.get("unauthorized_token") or "",
        }
        if not all(markers.values()):
            return None
        hits[label] = _child_directory_hits(requests, row["prompt"], markers)
    workers = all((_calls_return_text(rows[label]) for label in expected))
    if scenario_id.endswith("_control"):
        parent, empty = (hits["parent-grant"], hits["child-empty"])
        return {
            "workers": workers,
            "parent_route": parent is not None and parent["result_count"] >= 3,
            "parent_add_dir_live": bool(parent and parent["main"] and parent["extra"]),
            "empty_child_route": empty is not None and empty["result_count"] >= 3,
            "empty_child_main_live": bool(empty and empty["main"]),
        }
    target, empty = (hits["child-target"], hits["child-empty-target"])
    return {
        "workers": workers,
        "child_wire": target is not None and target["result_count"] >= 3,
        "main_control": bool(target and target["main"]),
        "inherits_extra": bool(target and target["extra"]),
        "unauthorized_sibling_denied": bool(target and target["unauthorized"] is False),
        "empty_child_wire": empty is not None and empty["result_count"] >= 3,
        "empty_child_main_control": bool(empty and empty["main"]),
        "empty_without_add_dir_closed": bool(
            empty and empty["extra"] is False and (empty["unauthorized"] is False)
        ),
    }


def _child_directory_structured_facts(
    scenario_id: str, api_result: dict, plan: dict | None, requests: list[dict]
) -> dict[str, object] | None:
    """Project the child-inheritance observation into the scenario's typed facts."""
    if plan is None:
        return None
    rows = api_result.get("child_directory_rows")
    if not isinstance(rows, dict):
        return {scenario_id: {"observed": False, "supported": False, "checks": []}}
    by_label = {str(item.get("label")): item for item in plan["variants"]}
    merged = {}
    for label, row in rows.items():
        variant = by_label.get(label, {})
        merged[label] = {
            **row,
            "main_token": variant.get("main_token"),
            "extra_token": variant.get("extra_token"),
            "unauthorized_token": variant.get("unauthorized_token"),
        }
    checks = _child_directory_checks(scenario_id, merged, requests)
    if checks is None:
        return {scenario_id: {"observed": False, "supported": False, "checks": []}}
    return {
        scenario_id: {"observed": True, "supported": True, "checks": [bool(item) for item in checks.values()]}
    }


_CLIPBOARD_SCENARIOS = frozenset({"copy_utf8_clipboard_bytes", "copy_utf8_clipboard_control"})
_CLIPBOARD_RECIPE_KEY = "clipboard_api_variants"


def _clipboard_plan(
    scenario_id: str, execution_parameters: tuple[tuple[str, object], ...]
) -> dict[str, object] | None:
    """Build the clipboard plan and recover each variant's expected reply.

    The expected bytes are the stub's own ``reply_content``: the model returns
    it, ``/copy`` must hand exactly those UTF-8 bytes to the helper.  Reading it
    from the declaration keeps the driver from carrying a second copy that could
    drift from the stimulus.
    """
    if scenario_id not in _CLIPBOARD_SCENARIOS:
        return None
    declared = dict(execution_parameters).get(_CLIPBOARD_RECIPE_KEY)
    if not declared:
        return None
    variants = [dict(_unfreeze(variant)) for variant in declared]
    for variant in variants:
        stub = variant.get("stub")
        reply = dict(stub).get("reply_content") if isinstance(stub, dict) else None
        if not reply or not variant.get("capture") or (not variant.get("meta")):
            return None
        variant["expected_reply"] = reply
    return {"variants": variants}


def _clipboard_declared_stub(execution_parameters: tuple[tuple[str, object], ...]) -> dict | None:
    """Return the gateway stub declared inside the clipboard variant.

    These recipes carry their stub per variant instead of at the top level.
    Only a single-variant shape is lifted: with several variants the rounds
    would have to be sequenced per variant, which this contract never declares.
    """
    declared = dict(execution_parameters).get(_CLIPBOARD_RECIPE_KEY)
    variants = [dict(_unfreeze(variant)) for variant in declared or ()]
    if len(variants) != 1:
        return None
    stub = variants[0].get("stub")
    return dict(stub) if isinstance(stub, dict) else None


def _clipboard_locale_is_non_utf8(row: dict) -> bool:
    """Report whether the non-UTF-8 C locale stimulus actually formed."""
    reported = row.get("locale") or {}
    preferred = str(reported.get("preferred") or "").lower()
    return (
        reported.get("lc_all") == "C"
        and int(reported.get("utf8_mode") or 0) == 0
        and ("utf" not in preferred)
    )


def _clipboard_checks(rows: dict, expected: str) -> dict[str, bool] | None:
    """Compare the captured clipboard bytes against the declared reply."""
    if len(rows) != 1:
        return None
    row = next(iter(rows.values()))
    chat = row.get("chat") or {}
    dispatch = row.get("dispatch") or {}
    try:
        captured = bytes.fromhex(row.get("capture_hex") or "")
    except ValueError:
        captured = b""
    expected_bytes = expected.encode("utf-8")
    return {
        "chat_exact": chat.get("return") == expected,
        "dispatch_public_string": dispatch.get("return_is_str") is True,
        "helper_invoked": row.get("capture_present") is True,
        "bytes_exact": captured == expected_bytes,
        "length_exact": row.get("capture_length") == len(expected_bytes),
        "single_helper": isinstance(row.get("helper_meta"), dict),
    }


def _clipboard_structured_facts(
    scenario_id: str, api_result: dict, plan: dict | None
) -> dict[str, object] | None:
    """Project the clipboard observation into the scenario's typed facts.

    The control additionally gates on the locale stimulus: if the interpreter
    never landed in a non-UTF-8 locale, the byte comparison proves nothing and
    the point is reported unobserved rather than passed or failed.
    """
    if plan is None:
        return None
    rows = api_result.get("clipboard_rows")
    unobserved = {scenario_id: {"observed": False, "supported": False, "checks": []}}
    if not isinstance(rows, dict) or not rows:
        return unobserved
    expected = str(plan["variants"][0].get("expected_reply") or "")
    checks = _clipboard_checks(rows, expected)
    if checks is None:
        return unobserved
    if scenario_id.endswith("_control"):
        row = next(iter(rows.values()))
        if not _clipboard_locale_is_non_utf8(row):
            return unobserved
        checks = {"c_locale": True, **checks}
    return {
        scenario_id: {"observed": True, "supported": True, "checks": [bool(item) for item in checks.values()]}
    }


_MEMORY_RESIDENCY_SCENARIOS = frozenset(
    {"memory_query_topk_residency", "memory_query_topk_residency_control"}
)
_MEMORY_RESIDENCY_MARGIN_BYTES = 4 * 1024 * 1024


def _memory_residency_plan(
    scenario_id: str, execution_parameters: tuple[tuple[str, object], ...]
) -> dict[str, object] | None:
    """Build the residency plan from the declared variants."""
    if scenario_id not in _MEMORY_RESIDENCY_SCENARIOS:
        return None
    declared = dict(execution_parameters).get(_CLIPBOARD_RECIPE_KEY)
    if not declared:
        return None
    variants = [dict(_unfreeze(variant)) for variant in declared]
    required = ("markers", "warm_query", "decoy", "entries", "target_prompt", "rss_probe_bytes")
    for variant in variants:
        if any((key not in variant for key in required)):
            return None
        variant["markers"] = list(variant["markers"])
    return {"variants": variants}


def _memory_residency_stub(execution_parameters: tuple[tuple[str, object], ...]) -> dict | None:
    """Lift the per-variant gateway stub, repeated once per declared variant.

    Like the clipboard recipes the stub lives inside the variant.  Every variant
    declares the same stub, so the shared sequence is repeated to keep a later
    variant from receiving ``phase-done`` on its first request.
    """
    declared = dict(execution_parameters).get(_CLIPBOARD_RECIPE_KEY)
    variants = [dict(_unfreeze(variant)) for variant in declared or ()]
    if not variants:
        return None
    stub = variants[0].get("stub")
    if not isinstance(stub, dict):
        return None
    stub = dict(stub)
    rounds = stub.get("rounds")
    if isinstance(rounds, list) and len(variants) > 1:
        stub["rounds"] = [
            json.loads(json.dumps(entry)) for _index in range(len(variants)) for entry in rounds
        ]
    return stub


def _memory_topk_facts(requests: list[dict], prompt: str, markers: list[str], decoy: str) -> dict:
    """Derive the ranked-result facts from the recorded tool results."""
    texts = [
        text
        for request in requests
        if (request.get("probes") or {}).get(prompt)
        for text in request.get("tool_result_texts") or ()
    ]
    blob = "\n".join((str(text) for text in texts))
    positions = [blob.find(marker) for marker in markers]
    complete = all((position >= 0 for position in positions))
    return {
        "tool_result": bool(blob),
        "all_markers": complete,
        "ordered": (
            all((left < right for left, right in zip(positions, positions[1:]))) if complete else False
        ),
        "one_each": all((blob.count(marker) == 1 for marker in markers)),
        "no_decoy": decoy not in blob,
    }


def _requests_for_span(requests: list[dict], span: object) -> list[dict]:
    """Slice the gateway ledger down to one variant's own requests.

    Every variant sends the same probe token, so the token cannot identify the
    variant; the ledger boundaries recorded around each variant can.  Without
    the slice the ranked-result facts would be derived from both variants'
    results concatenated.
    """
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return requests
    start, end = span
    if not isinstance(start, int) or not isinstance(end, int) or start > end:
        return requests
    return requests[start:end]


def _memory_residency_rss_readable(row: dict) -> bool:
    """Report whether the RSS profile itself was captured."""
    rss = row.get("rss") or {}
    return (
        all((isinstance(rss.get(key), int) for key in ("baseline", "peak", "after", "delta")))
        and int(rss.get("samples") or 0) > 0
    )


def _memory_residency_sampler_allocated(row: dict, expected_bytes: int) -> bool:
    """Report whether the self-check allocated the declared block.

    This is the target's gate.  The target is gated on the control, which is
    what proves the sampler can actually see an allocation; here it is enough
    that the probe ran and allocated what the recipe declared.
    """
    probe = row.get("rss_sampler_probe") or {}
    return int(probe.get("allocated") or 0) == int(expected_bytes)


def _memory_residency_sampler_observed(row: dict) -> bool:
    """Report whether the sampler measured its own allocation.

    This stricter gate belongs to the zero-weight control, whose job is to prove
    RSS is observable at all.  It cannot be applied to the large-archive variant:
    once the heap is already hundreds of megabytes the kernel satisfies a 16 MiB
    allocation from pages the process already holds, so the delta legitimately
    stays near zero even though RSS itself is perfectly readable.
    """
    probe = row.get("rss_sampler_probe") or {}
    expected = int(probe.get("allocated") or 0)
    return (
        isinstance(probe.get("delta"), int)
        and expected > 0
        and (probe["delta"] >= expected // 2)
        and (int(probe.get("samples") or 0) > 0)
    )


def _memory_residency_checks(
    scenario_id: str, rows: dict, plan: dict, requests: list[dict]
) -> dict[str, bool] | None:
    """Derive the residency checks, or None when the point is unobservable."""
    by_label = {str(item.get("label")): item for item in plan["variants"]}
    if set(rows) != set(by_label):
        return None
    if not all((_memory_residency_rss_readable(row) for row in rows.values())):
        return None
    if scenario_id.endswith("_control"):
        if not all((_memory_residency_sampler_observed(row) for row in rows.values())):
            return None
    elif not all(
        (
            _memory_residency_sampler_allocated(row, by_label[label]["rss_probe_bytes"])
            for label, row in rows.items()
        )
    ):
        return None
    topk = {}
    for label, row in rows.items():
        variant = by_label[label]
        topk[label] = _memory_topk_facts(
            _requests_for_span(requests, row.get("request_span")),
            variant["target_prompt"],
            variant["markers"],
            variant["decoy"],
        )

    def calls_returned(row: dict) -> bool:
        return (row.get("index_call") or {}).get("return_is_str") is True and (row.get("measured") or {}).get(
            "return_is_str"
        ) is True

    if scenario_id.endswith("_control"):
        label, row = next(iter(rows.items()))
        return {
            "worker": True,
            "public_calls": calls_returned(row),
            "rss_available": True,
            "sampler_live": True,
            **{f"topk_{key}": value for key, value in topk[label].items()},
        }
    if len(rows) != 2:
        return None
    ordered = sorted(rows, key=lambda label: int(rows[label]["entries"]))
    small_label, large_label = (ordered[0], ordered[-1])
    if int(rows[small_label]["entries"]) >= int(rows[large_label]["entries"]):
        return None
    small_delta = int((rows[small_label]["rss"] or {})["delta"])
    large_delta = int((rows[large_label]["rss"] or {})["delta"])
    functional = all(
        (
            facts["tool_result"]
            and facts["all_markers"]
            and facts["ordered"]
            and facts["one_each"]
            and facts["no_decoy"]
            for facts in topk.values()
        )
    )
    comparable = ("all_markers", "ordered", "one_each", "no_decoy")
    return {
        "workers": all((calls_returned(row) for row in rows.values())),
        "ranked_results": functional,
        "same_topk": [topk[small_label][key] for key in comparable]
        == [topk[large_label][key] for key in comparable],
        "residency_bounded": large_delta <= small_delta + _MEMORY_RESIDENCY_MARGIN_BYTES,
    }


def _memory_residency_structured_facts(
    scenario_id: str, api_result: dict, plan: dict | None, requests: list[dict]
) -> dict[str, object] | None:
    """Project the residency observation into the scenario's typed facts."""
    if plan is None:
        return None
    rows = api_result.get("memory_residency_rows")
    unobserved = {scenario_id: {"observed": False, "supported": False, "checks": []}}
    if not isinstance(rows, dict) or not rows:
        return unobserved
    checks = _memory_residency_checks(scenario_id, rows, plan, requests)
    if checks is None:
        return unobserved
    row: dict[str, object] = {
        "observed": True,
        "supported": True,
        "checks": [bool(item) for item in checks.values()],
    }
    if "public_calls" in checks:
        row["public_calls"] = bool(checks["public_calls"])
    return {scenario_id: row}


def _mcp_peer_variant(execution_parameters: tuple[tuple[str, object], ...]):
    """The single declared peer variant, or None.

    These points declare their whole stimulus inside the variant - the
    commands, the config seed, the gateway stub and the marker file - rather
    than at the top level, because the stage1 smoke layer drives one isolated
    child per variant.  A driver that reads only the top level starts a session with
    no commands at all: the peer is never configured, never launched, and the
    point reports its structured facts unobserved on a run that had no
    stimulus.
    """
    declared = _unfreeze(dict(execution_parameters).get("mcp_peer_variants"))
    if not isinstance(declared, list) or len(declared) != 1:
        return None
    variant = declared[0]
    return variant if isinstance(variant, dict) else None


def _run_api_probe(
    artifact: Path, home: Path, workspace: Path, env: dict[str, str], timeout: int
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run the public API probe in the same isolated evaluation image."""
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
        f"{home}:/sbh-home:rw",
        "-v",
        f"{workspace}:/sbh-workspace:rw",
        "-w",
        "/sbh-workspace",
        "-e",
        "HOME=/sbh-home",
        "-e",
        "YUYAO_HOME=/sbh-home/.yuyao",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "PYTHONPATH=/sbh-artifact/src",
    ]
    for key, value in env.items():
        if key.startswith("D2A_"):
            command.extend(("-e", f"{key}={value}"))
    command.extend(("-e", f"SBH_SCENARIO_ID={env.get('SBH_SCENARIO_ID', '')}"))
    for name in (
        "SBH_MEMORY_FORGET_PLAN",
        "SBH_CHILD_DIRECTORY_PLAN",
        "SBH_CLIPBOARD_PLAN",
        "SBH_MEMORY_RESIDENCY_PLAN",
        "SBH_RESIDENCY_WORKER_SOURCE",
        "SBH_WIRE_RETENTION_COMMANDS",
        "SBH_WIRE_PREMSG_COUNT",
        "SBH_SESSION_DENY",
        "SBH_SESSION_FACT_COMMANDS",
        "SBH_SESSION_PREMSGS",
        "SBH_HELP_PROBE",
    ):
        if env.get(name):
            command.extend(("-e", f"{name}={env[name]}"))
    if env.get("SBH_CLIPBOARD_PLAN"):
        command.extend(
            ("-e", "LC_ALL=C", "-e", "LANG=C", "-e", "PYTHONUTF8=0", "-e", "PYTHONCOERCECLOCALE=0")
        )
    command.extend((image, "python3", "-c", _api_probe_source()))
    try:
        return (
            subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            ),
            False,
        )
    except subprocess.TimeoutExpired as exc:
        return (subprocess.CompletedProcess(command, -1, exc.stdout or "", exc.stderr or ""), True)
_API_DRIVEN_STATE_SCENARIOS = frozenset(
    {"plugin_declared_command_reaches_wire", "plugin_toplevel_command_control"}
)


def _interactive_step_budget(commands: tuple[str, ...]) -> int:
    """Derive a per-session step cap that every declared command can reach.

    The artifact's own limit (200) lets a stub with no declared rounds spend the
    whole run on one command: the gateway keeps answering with tool calls, so
    the later commands are typed but their requests never reach the wire.

    The cap cannot be the stage1 smoke layer's 20 either, because that value is per
    public-API call while this budget is per session, and startup alone costs a
    different amount on each artifact.  Allow a startup allowance plus room per
    declared command.

    This bounds the loop but does not rescue an artifact that never terminates
    its first turn - raising the cap only buys more iterations of the same
    non-terminating turn, which is product behaviour rather than a driver gap.
    """
    startup_allowance = 30
    per_command = 10
    return max(60, startup_allowance + per_command * max(1, len(commands)))


def _interactive_container_command(
    artifact: Path,
    home: Path,
    workspace: Path,
    image: str,
    model_base_url: str,
    max_steps: int = 60,
    deny_rules: tuple[str, ...] = (),
) -> list[str]:
    """Build the interactive state container command with the fixture cwd bound."""
    entrypoint = [
        "python3",
        "/sbh-artifact/src/yuyao/__main__.py",
        "--yolo",
        "--stream",
        "off",
        "--max-steps",
        str(max_steps),
    ]
    for rule in deny_rules:
        entrypoint.extend(("--deny", str(rule)))
    return [
        "docker",
        "run",
        *container_memory_flags(),
        "--rm",
        "--init",
        "-it",
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
        f"{home}:/sbh-home:rw",
        "-v",
        f"{workspace}:/sbh-workspace:rw",
        "-w",
        "/sbh-workspace",
        "-e",
        "HOME=/sbh-home",
        "-e",
        "YUYAO_HOME=/sbh-home/.yuyao",
        "-e",
        "PYTHONDONTWRITEBYTECODE=1",
        "-e",
        "PYTHONPATH=/sbh-artifact/src",
        "-e",
        f"D2A_MODEL_BASE_URL={model_base_url}",
        "-e",
        "D2A_MODEL_NAME=litellm_proxy/mock",
        "-e",
        "D2A_MODEL_API_KEY=sk-mock-test",
        image,
        *entrypoint,
    ]


def _read(master: int, seconds: float) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            data.extend(os.read(master, 65536))
        except OSError:
            break
    return bytes(data)


_ANSI_RE = re.compile("\\x1b\\[[0-9;?]*[ -/]*[@-~]")
_PROMPT_MARKERS = ("›", "»", "⟩", "❯", "yuyao>", "yuyao >", "yuyao:", "You:", "You>", "you>")
_PROMPT_RE = re.compile(
    "(?:yuyao|You|you)[ \\t]*(?:\\[[^\\]\\n]{0,64}\\])?[ \\t]*[>:›»⟩❯]|^[ \\t]*[>›»⟩❯][ \\t]*$", re.MULTILINE
)


def _prompt_visible(text: str) -> bool:
    """True when the text ends a rendered input prompt in any observed form."""
    return any((marker in text for marker in _PROMPT_MARKERS)) or bool(_PROMPT_RE.search(text))


def _read_until_startup_prompt(master: int, seconds: float) -> bytes:
    """Read the startup banner up to the first rendered input prompt.

    The banner is still painting for an indeterminate time after launch.  A
    command written before the prompt exists is absorbed into the banner and
    then arrives glued to the following command, so the artifact reports the
    concatenation as one malformed input.
    """
    data = bytearray()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        try:
            data.extend(os.read(master, 65536))
        except OSError:
            break
        plain = _ANSI_RE.sub("", bytes(data).decode("utf-8", errors="replace"))
        if _prompt_visible(plain):
            data.extend(_read(master, 0.3))
            return bytes(data)
    return bytes(data)


def _collapse_wrapped_echo(text: str) -> str:
    """Join lines so a terminal-wrapped command echo matches its input.

    A command longer than the terminal width is echoed across several lines.
    Searching for the verbatim command then never matches, the helper below
    never sees its prompt, and the next command is written while the previous
    one is still being read - which the artifact receives as one concatenated
    line (``unknown permission mode 'yolo\\n/mcp add ...'``).
    """
    return re.sub("[ \\t]*\\n[ \\t]*", "", text)


def _settle_after(window: object, master: int, prior: bytes) -> bytes:
    """Grant a daemonising command a bounded start window; return bytes read.

    A recipe declares the window in one of two shapes, and both are in active
    use, so both are supported here:

    ``('2', '1.0')``
        A plain number of seconds: sleep, read nothing.

    ``('2', (('deadline_s', '5.0'), ('until_token', 'FORKDIR_9143')))``
        A deadline plus a token to wait *for*.  Poll the pty until the token is
        rendered, then return immediately; fall back to the deadline if it never
        appears.  This is strictly better than a blind sleep — a fork that is
        serving after 200 ms does not cost the full window, and one that needs
        4 s is not read too early.
    """
    if isinstance(window, dict):
        deadline_raw = window.get("deadline_s")
        token = window.get("until_token")
        try:
            seconds = min(float(deadline_raw), 10.0)
        except (TypeError, ValueError):
            seconds = 10.0
        if not isinstance(token, str) or not token:
            time.sleep(seconds)
            return b""
        data = bytearray()
        deadline = time.monotonic() + seconds
        needle = token.encode("utf-8")
        if needle in prior:
            return b""
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            data.extend(chunk)
            if needle in prior + bytes(data):
                break
        return bytes(data)
    try:
        time.sleep(min(float(window), 10.0))
    except (TypeError, ValueError):
        time.sleep(10.0)
    return b""


def _echo_tail(plain: str, prior_plain: str, command: str) -> str | None:
    """What the product rendered after echoing ``command``, or None if unseen.

    The echo may be wrapped across terminal lines, so a newline-collapsed view
    is tried before giving up.
    """
    position = plain.rfind(command, max(0, len(prior_plain) - len(command)))
    if position >= 0:
        return plain[position + len(command) :]
    folded = _collapse_wrapped_echo(plain)
    folded_prior = _collapse_wrapped_echo(prior_plain)
    position = folded.rfind(command, max(0, len(folded_prior) - len(command)))
    if position >= 0:
        return folded[position + len(command) :]
    return None


def _read_until_command_prompt(master: int, prior: bytes, command: str, seconds: float) -> bytes:
    """Read until a prompt rendered after the current input is visible.

    Interactive state recipes must not enqueue their next slash command while
    the previous command is still being processed.  A fixed sleep is not a
    synchronization primitive: on a busy artifact it can concatenate inputs
    (for example ``/compact`` and ``/exit``).  Count the rendered prompt
    occurrences already present and return once a new one arrives.
    """
    data = bytearray()
    deadline = time.monotonic() + seconds
    quiet_seconds = 3.0
    last_output = time.monotonic()
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            if data and time.monotonic() - last_output >= quiet_seconds:
                combined = (prior + bytes(data)).decode("utf-8", errors="replace")
                plain = _ANSI_RE.sub("", combined).replace("\r", "\n")
                prior_plain = _ANSI_RE.sub("", prior.decode("utf-8", errors="replace")).replace("\r", "\n")
                if _echo_tail(plain, prior_plain, command) is not None:
                    return bytes(data)
            continue
        try:
            data.extend(os.read(master, 65536))
        except OSError:
            break
        last_output = time.monotonic()
        combined = (prior + bytes(data)).decode("utf-8", errors="replace")
        plain = _ANSI_RE.sub("", combined).replace("\r", "\n")
        prior_plain = _ANSI_RE.sub("", prior.decode("utf-8", errors="replace")).replace("\r", "\n")
        tail = _echo_tail(plain, prior_plain, command)
        if tail is not None and _prompt_visible(tail):
            return bytes(data)
    return bytes(data)


def _json_config(home: Path) -> dict[str, object]:
    for path in (home / ".yuyao" / "config.json", home / "config.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def _freeze(value: object) -> object:
    """Convert recorder JSON into the closed tuple-only State fact type."""
    if isinstance(value, dict):
        return tuple(sorted(((str(key), _freeze(item)) for key, item in value.items())))
    if isinstance(value, list):
        return tuple((_freeze(item) for item in value))
    if value is None or type(value) in {str, int, bool}:
        return value
    return str(value)


def _unfreeze(value: object) -> object:
    """Convert the closed tuple representation back to gateway config data.

    An empty tuple stays a list.  ``all()`` is vacuously true for it, so the
    pair-shaped test below would otherwise turn every empty sequence into an
    empty mapping - and a gateway round declared as ``[]`` (a deliberate
    no-tool turn that lets the exchange finish) would arrive as ``{}``.
    """
    if isinstance(value, tuple):
        if value and all(
            (isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in value)
        ):
            return {str(key): _unfreeze(item) for key, item in value}
        return [_unfreeze(item) for item in value]
    return value


def _interactive_cwd(screen: str) -> str:
    """Extract the artifact-reported cwd across supported CLI prompt variants.

    Each product announces the directory in its own words - "working
    directory: /x", "working directory is now /x", "cwd -> /x" - and the
    wording is not part of any contract.  A phrase this does not recognise
    falls back to the default, which reads as the directory having been reset:
    a point asserting the cwd survived a command then fails on a session that
    never moved.  New spellings therefore belong here, not in the judges.
    """
    screen = _ANSI_RE.sub("", screen)
    matches = re.findall(
        "working directory(?:\\s+is\\s+now|):\\s*(/\\S*)|working directory\\s+is\\s+now\\s+(/\\S*)|cwd\\s*(?:->|:|=)\\s*(/\\S*)",
        screen,
        re.IGNORECASE,
    )
    paths = [left or middle or right for left, middle, right in matches]
    return paths[-1].strip() if paths else "/sbh-workspace"


def _console_after_startup(text: str, commands: tuple[str, ...] = ()) -> str:
    """Strip terminal formatting and drop the banner from the judged console.

    The colour codes go first.  A judge counting the commands a listing offers
    matches names like "/status", and an escape sequence between the slash and
    the word breaks every one of them and the listing is undercounted.  The
    stage1 smoke layer binds a colourless buffer, so its console
    is plain text and its judges are written for that.

    A point of the form "this command must produce something related to its
    own name" is answered by what the command printed.  The banner advertises
    the product's own features - "! to run a shell command", "Esc stops the
    run" - so keeping it lets a command that printed nothing at all match a
    keyword drawn from its own name, and the point then passes on the banner.
    The stage1 smoke layer binds a fresh console buffer after startup for exactly
    this reason.

    The cut is made at the echo of the first declared command rather than at
    any particular banner text: each product prints its own banner, and one of
    them ends where another does not.
    """
    text = _ANSI_RE.sub("", text)
    for command in commands:
        index = text.find(str(command))
        if index > 0:
            return text[index:]
    return text


def _observed_file_facts(
    root: Path, patterns: tuple[str, ...], boundary: Path | None = None
) -> dict[str, str]:
    """Read declared files or glob patterns without escaping the workspace.

    ``..`` in a declared path is meaningful, not an attack: a point that runs
    ``/cd sub`` and then asks whether memory landed in the *parent* names the
    file as ``../MEMORY.md`` relative to the child cwd - and several products
    keep memory at the session root, so the parent is exactly where it goes.
    The stage1 smoke layer resolves these against the live cwd and reads them.  Allow
    ``..`` but confine the resolved path to ``boundary`` (the workspace root),
    so a genuinely out-of-workspace path is still refused.
    """
    limit = (boundary or root).resolve()
    result: dict[str, str] = {}
    for pattern in patterns:
        candidate = Path(pattern)
        if candidate.is_absolute():
            continue
        matches = root.glob(pattern) if any((ch in pattern for ch in "*?[")) else (root / pattern,)
        for path in matches:
            try:
                resolved = path.resolve()
                resolved.relative_to(limit)
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            try:
                relative = os.path.relpath(resolved, root)
                result[relative] = resolved.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
    return result


def _pre_message_commands(execution_parameters: tuple[tuple[str, object], ...]) -> tuple[str, ...]:
    """Convert declared user pre-messages into real interactive inputs."""
    params = dict(execution_parameters)
    raw = params.get("premsgs", ())
    commands: list[str] = []
    if not isinstance(raw, tuple):
        return ()
    for item in raw:
        if not isinstance(item, tuple):
            continue
        message = dict(item)
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            commands.append(message["content"])
    return tuple(commands)


def _pre_message_records(execution_parameters: tuple[tuple[str, object], ...]) -> list[dict[str, object]]:
    """Return declared pre-messages as whole records, every role kept.

    ``_pre_message_commands`` keeps only the user content, because typed input
    can express nothing else.  A judge that reads the role sequence needs the
    assistant and system entries as well - dropping them turns the declared
    alternating baseline into a run of consecutive user roles, which is exactly
    the defect those scenarios are trying to detect.
    """
    raw = dict(execution_parameters).get("premsgs", ())
    if not isinstance(raw, tuple):
        return []
    records: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, tuple):
            continue
        message = {str(key): value for key, value in item if isinstance(key, str)}
        if message.get("role"):
            records.append(message)
    return records


def _apply_git_fixture(workspace: Path, execution_parameters: tuple[tuple[str, object], ...]) -> None:
    """Commit the seeded files as a baseline, then write the post-baseline set.

    ``/diff`` and ``/undo`` are defined over a git working tree, so a scenario
    that exercises them declares ``git_init``.  The seeded files become the
    committed baseline and ``post_files`` are written afterwards, which is what
    makes them read as the agent's uncommitted changes.

    ``git_branch_commit`` additionally commits the post-baseline set onto a
    second branch, leaving the working tree clean: the situation ``/review`` and
    ``/security-review`` are specified over is "the current *branch* differs",
    not "the working tree differs".  Two real branches are created rather than
    one extra commit because without a remote the base of "branch changes" is
    ambiguous, and a resolvable ``origin/HEAD`` is published because Claude
    Code's own security review runs ``git diff origin/HEAD...`` with no
    fallback - a fixture that omits the ref would fail an implementation that
    copies that approach for reasons that are the fixture's fault.
    """
    parameters = dict(execution_parameters)
    if not parameters.get("git_init"):
        return
    identity = ("-c", "user.email=fixture@example.invalid", "-c", "user.name=SBH Fixture")

    def run(*arguments: str) -> None:
        subprocess.run(
            ("git", *identity, *arguments),
            cwd=workspace,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    run("init", "-q")
    run("add", "-A")
    run("commit", "-q", "-m", "baseline", "--allow-empty")
    post_files = _unfreeze(parameters.get("post_files") or ())
    if isinstance(post_files, dict):
        entries = list(post_files.items())
    elif isinstance(post_files, list):
        entries = [tuple(item) for item in post_files if isinstance(item, (list, tuple)) and len(item) == 2]
    else:
        entries = []
    if parameters.get("git_branch_commit"):
        run("branch", "-M", "main")
        run("checkout", "-q", "-b", "review-subject")
    for relative, content in entries:
        path = workspace / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
    if parameters.get("git_branch_commit"):
        run("add", "-A")
        run("commit", "-q", "-m", "branch change")
        run("update-ref", "refs/remotes/origin/main", "refs/heads/main")
        run("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def _snapshot_command_index(execution_parameters: tuple[tuple[str, object], ...]) -> int | None:
    """Return the command index after which the observed files are snapshotted.

    A requirement of the form "this *segment* must not touch that file" cannot
    be decided from the final state alone, which is why the fixture carries a
    mid-sequence snapshot.  Without it a judge has to recognise the
    file's *format* to infer what wrote it, and the format is each build's own
    choice: counting memory entries as lines starting with ``-`` reads zero from
    a build that writes ``## memory-flush <ISO>``, turning a real failure into
    a non-observable result.
    """
    value = dict(execution_parameters).get("snap_after")
    return value if isinstance(value, int) and (not isinstance(value, bool)) else None


def _run_one() -> dict[str, object]:
    from doc2harness.benchmark.definition import definition_digest
    from doc2harness.benchmark.dispatch import load_scenario_owner
    from doc2harness.benchmark.harnesses.state.adapter import build_state_execution_plan
    from doc2harness.benchmark.harnesses.state.contracts import StateObservation

    scenario_id = os.environ["SBH_SCENARIO_ID"]
    owner = load_scenario_owner(scenario_id)
    if ".benchmark.harnesses.state." not in owner.module:
        raise RuntimeError("state driver received a non-state scenario")
    scenario = owner.scenario
    plan = build_state_execution_plan(scenario)
    gateway = mock_gateway
    reply_content = "STATE_MOCK_REPLY_7731"
    execution_parameters = dict(plan.execution_parameters)
    declared_stub = _unfreeze(execution_parameters.get("stub"))
    peer_variant = _mcp_peer_variant(plan.execution_parameters)
    if declared_stub is None and isinstance(peer_variant, dict):
        declared_stub = peer_variant.get("stub")
    if isinstance(declared_stub, dict) and scenario_id in _CHILD_DIRECTORY_SCENARIOS:
        declared_variants = dict(scenario.recipe.execution_parameters).get(_CHILD_DIRECTORY_RECIPE_KEY)
        declared_stub = _resolve_child_directory_stub(declared_stub, len(declared_variants or ()) or 1)
    if declared_stub is None and scenario_id in _CLIPBOARD_SCENARIOS:
        declared_stub = _clipboard_declared_stub(scenario.recipe.execution_parameters)
    if declared_stub is None and scenario_id in _MEMORY_RESIDENCY_SCENARIOS:
        declared_stub = _memory_residency_stub(scenario.recipe.execution_parameters)
    if isinstance(declared_stub, dict) and (not declared_stub.get("mode")):
        declared_stub = {
            "mode": "plain_no_tool",
            "reply_content": "Summary: task context compacted. STUB_SUMMARY_5150",
            **declared_stub,
        }
    if isinstance(declared_stub, dict):
        cfg = gateway.new_cfg(**declared_stub)
    elif scenario_id in {"agent_api_close_retry_incomplete_control", "agent_api_close_retry_incomplete"}:
        cfg = gateway.new_cfg(
            mode="seq_probe",
            probe_tokens=["CLOSE_ROUTE_6501"],
            rounds=[
                [
                    {
                        "name": "bash",
                        "command": "python3 /sbh-workspace/repo/close_helper.py",
                        "run_in_background": True,
                    }
                ],
                [{"name": "__finish__"}],
            ],
        )
    else:
        cfg = gateway.new_cfg(mode="fixed_reply", reply_content=reply_content)
    server = gateway.ThreadingHTTPServer(("0.0.0.0", 0), gateway.Handler)
    server.cfg = cfg
    server.daemon_threads = True
    server.block_on_close = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="sbh-state-production-") as temporary:
        root = Path(temporary)
        home, workspace, output = (root / "home", root / "workspace", root / "out")
        home.mkdir()
        workspace.mkdir()
        output.mkdir()
        for relative, content in plan.fixture.seed_files:
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        for relative, content in plan.fixture.home_seed_files:
            path = home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _apply_git_fixture(workspace, plan.execution_parameters)
        help_probe_declared = bool(dict(plan.execution_parameters).get("probe_help"))
        snapshot_index = _snapshot_command_index(plan.execution_parameters)
        files_mid: dict[str, str] = {}
        settle_raw = _unfreeze(dict(plan.execution_parameters).get("settle_after") or ())
        settle_windows = dict(settle_raw) if isinstance(settle_raw, dict) else {}
        config_snapshot_index = dict(plan.execution_parameters).get("cfg_snap_after")
        if not isinstance(config_snapshot_index, int) or isinstance(config_snapshot_index, bool):
            config_snapshot_index = None
        config_mid: dict[str, object] = {}
        _declared_permissions = dict(plan.execution_parameters)
        if _declared_permissions.get("deny") or _declared_permissions.get("allow"):
            config_dir = home / ".yuyao"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / "config.json"
            try:
                config_value = json.loads(config_path.read_text(encoding="utf-8"))
                if not isinstance(config_value, dict):
                    config_value = {}
            except (OSError, UnicodeError, json.JSONDecodeError):
                config_value = {}
            if _declared_permissions.get("deny"):
                config_value["permissions.deny"] = [str(rule) for rule in _declared_permissions["deny"]]
            if _declared_permissions.get("allow"):
                config_value["permissions.allow"] = [str(rule) for rule in _declared_permissions["allow"]]
            config_path.write_text(
                json.dumps(config_value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "YUYAO_HOME": str(home / ".yuyao"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(Path(os.environ["SBH_ARTIFACT_PATH"]) / "src"),
                "D2A_MODEL_BASE_URL": f"http://host.docker.internal:{server.server_address[1]}/v1",
                "D2A_MODEL_NAME": "litellm_proxy/mock",
                "D2A_MODEL_API_KEY": "sk-mock-test",
            }
        )
        if help_probe_declared:
            env["SBH_HELP_PROBE"] = "1"
        api_result: dict[str, object] = {}
        child_directory_plan = _child_directory_plan(scenario_id, scenario.recipe.execution_parameters)
        if child_directory_plan is not None:
            env["SBH_CHILD_DIRECTORY_PLAN"] = json.dumps(child_directory_plan, sort_keys=True)
        clipboard_plan = _clipboard_plan(scenario_id, scenario.recipe.execution_parameters)
        if clipboard_plan is not None:
            env["SBH_CLIPBOARD_PLAN"] = json.dumps(clipboard_plan, sort_keys=True)
        residency_plan = _memory_residency_plan(scenario_id, scenario.recipe.execution_parameters)
        if residency_plan is not None:
            env["SBH_MEMORY_RESIDENCY_PLAN"] = json.dumps(residency_plan, sort_keys=True)
            env["SBH_RESIDENCY_WORKER_SOURCE"] = _MEMORY_RESIDENCY_WORKER
        wire_commands = (
            _pre_message_commands(plan.execution_parameters) + plan.commands
            if (scenario_id in _API_DRIVEN_STATE_SCENARIOS)
            else ()
        )
        if wire_commands:
            env["SBH_WIRE_RETENTION_COMMANDS"] = json.dumps([str(item) for item in wire_commands])
            env["SBH_WIRE_PREMSG_COUNT"] = str(len(_pre_message_commands(plan.execution_parameters)))
            declared_deny = dict(plan.execution_parameters).get("deny")
            if declared_deny:
                env["SBH_SESSION_DENY"] = json.dumps([str(rule) for rule in _unfreeze(declared_deny) or ()])
        session_fact_commands = ()
        if session_fact_commands:
            env["SBH_SESSION_FACT_COMMANDS"] = json.dumps([str(item) for item in session_fact_commands])
            premsgs = _pre_message_records(plan.execution_parameters)
            if premsgs:
                env["SBH_SESSION_PREMSGS"] = json.dumps(premsgs)
        if (
            scenario_id.startswith("agent_api_")
            or False
            or child_directory_plan is not None
            or (clipboard_plan is not None)
            or (residency_plan is not None)
            or bool(wire_commands)
            or bool(session_fact_commands)
            or help_probe_declared
        ):
            completed, timed_out = _run_api_probe(
                Path(os.environ["SBH_ARTIFACT_PATH"]), home, workspace, env, min(plan.timeout_seconds, 120)
            )
            returncode = completed.returncode
            screen = bytearray(
                "\n".join(
                    (
                        line
                        for line in (completed.stdout or "").splitlines()
                        if not line.startswith(_API_MARKER)
                    )
                ).encode()
            )
            screen.extend((completed.stderr or "").encode())
            for line in (completed.stdout or "").splitlines():
                if line.startswith(_API_MARKER):
                    try:
                        parsed = json.loads(line[len(_API_MARKER) :])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed = {}
                    if isinstance(parsed, dict):
                        api_result.update(parsed)
        else:
            image = os.environ.get("SBH_EVALUATION_IMAGE", "doc2harness/evaluation:candidate")
            commands = _pre_message_commands(plan.execution_parameters) + plan.commands
            command = _interactive_container_command(
                Path(os.environ["SBH_ARTIFACT_PATH"]),
                home,
                workspace,
                image,
                f"http://host.docker.internal:{server.server_address[1]}/v1",
                _interactive_step_budget(commands),
                deny_rules=tuple((str(rule) for rule in _declared_permissions.get("deny") or ())))
            master, slave = pty.openpty()
            try:
                import fcntl
                import struct
                import termios

                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
            except (ImportError, OSError):
                pass
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            screen = bytearray(_read_until_startup_prompt(master, 30.0))
            try:
                for index, item in enumerate(commands):
                    os.write(master, str(item).encode("utf-8") + b"\r")
                    screen.extend(
                        _read_until_command_prompt(
                            master, bytes(screen), str(item), min(float(plan.timeout_seconds), 60.0)
                        )
                    )
                    if index == snapshot_index:
                        snapshot_text = screen.decode("utf-8", errors="replace")
                        snapshot_root = workspace
                        snapshot_cwd = _interactive_cwd(snapshot_text)
                        if snapshot_cwd.startswith("/sbh-workspace/"):
                            snapshot_root = workspace / snapshot_cwd.removeprefix("/sbh-workspace/")
                        files_mid = _observed_file_facts(snapshot_root, plan.observed_files, workspace)
                    if str(index) in settle_windows:
                        screen.extend(_settle_after(settle_windows[str(index)], master, bytes(screen)))
                    if index == config_snapshot_index:
                        config_mid = _json_config(home)
                screen.extend(_read(master, min(float(plan.timeout_seconds), 60.0)))
                os.write(master, b"\r")
                screen.extend(_read(master, 2.0))
                os.write(master, b"/exit\r")
                screen.extend(_read(master, 5.0))
                try:
                    returncode = process.wait(timeout=10.0)
                except subprocess.TimeoutExpired:
                    os.write(master, b"\x04")
                    screen.extend(_read(master, 3.0))
                    returncode = process.wait(timeout=10.0)
                timed_out = False
            except (subprocess.TimeoutExpired, OSError):
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    returncode = process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    returncode = -1
            finally:
                os.close(master)
        trajectory = _trajectory(home) + "\n" + _trajectory(workspace)
        text = _console_after_startup(
            screen.decode("utf-8", errors="replace"), tuple((str(item) for item in plan.commands))
        )
        cwd_fact = _interactive_cwd(text)
        cwd_root = workspace
        if cwd_fact.startswith("/sbh-workspace/"):
            cwd_root = workspace / cwd_fact.removeprefix("/sbh-workspace/")
        files = _observed_file_facts(cwd_root, plan.observed_files, workspace)
        config = _json_config(home)
        returns = tuple((line.strip() for line in text.replace("\r", "\n").splitlines() if line.strip()))[
            -64:
        ]
        api_std = api_result.get("std")
        if isinstance(api_std, dict):
            std_fact: object = _freeze(api_std)
        else:
            std_fact = _freeze(
                {
                    "std": bool(api_result.get("std")),
                    "ok": bool(api_result.get("ok")),
                    "tb": bool(api_result.get("tb")),
                }
            )
        if isinstance(api_result.get("console"), str):
            text = api_result["console"]
        if isinstance(api_result.get("returns"), list):
            returns = tuple((str(item) for item in api_result["returns"]))
        api_cwd = api_result.get("cwd")
        if isinstance(api_cwd, str) and api_cwd:
            cwd_fact = api_cwd
        facts: dict[str, object] = {
            "result_present": bool(api_result.get("ok") or returns or cfg.get("requests")),
            "console": text,
            "std": std_fact,
            "returns": returns,
            "requests": tuple((_freeze(item) for item in cfg.get("requests", []))),
            "config": _freeze(
                api_result.get("config") if isinstance(api_result.get("config"), dict) else config
            ),
            "cwd": cwd_fact,
            "files": _freeze(files),
            "tree": _freeze(files),
            "messages": text,
            "files_mid": _freeze(files_mid),
            "config_mid": _freeze(config_mid),
            "error": (
                api_result["error"]
                if isinstance(api_result.get("error"), str) and api_result["error"]
                else ""
            ),
            "trajectory_text": trajectory,
            "structured_facts": tuple(),
            "process_reaped": returncode is not None,
            "workspace_clean": True,
        }
        for key in (
            "ctor",
            "contrast",
            "mcp",
            "messages",
            "messages_before",
            "message_count",
            "roles",
            "roles_before",
            "structured_facts",
            "wire_retention_returns",
            "help_probe",
            "help_claimed",
            "plans",
        ):
            if key in api_result:
                facts[key] = _freeze(api_result[key])
        child_directory_facts = _child_directory_structured_facts(
            scenario_id, api_result, child_directory_plan, list(cfg.get("requests") or ())
        )
        if child_directory_facts is not None:
            facts["structured_facts"] = _freeze(child_directory_facts)
            facts["result_present"] = True
        clipboard_facts = _clipboard_structured_facts(scenario_id, api_result, clipboard_plan)
        if clipboard_facts is not None:
            facts["structured_facts"] = _freeze(clipboard_facts)
            facts["result_present"] = True
        residency_facts = _memory_residency_structured_facts(
            scenario_id, api_result, residency_plan, list(cfg.get("requests") or ())
        )
        if residency_facts is not None:
            facts["structured_facts"] = _freeze(residency_facts)
            facts["result_present"] = True
        if isinstance(api_result.get("chat"), str) and api_result["chat"]:
            facts["returns"] = (api_result["chat"],)
        evidence = {
            "scenario_id": scenario_id,
            "commands": plan.commands,
            "returncode": returncode,
            "screen": text,
            "files": files,
        }
        observation = StateObservation(
            scenario_id=scenario_id,
            plan_digest=plan.plan_digest,
            artifact_id=os.environ["SBH_ARTIFACT_ID"],
            artifact_digest=os.environ["SBH_ARTIFACT_DIGEST"],
            backend_evidence_digest=_digest(evidence),
            fixture_verified=True,
            facts=tuple(sorted(facts.items())),
            process_tree_clean=not timed_out,
            workspace_clean=True,
        )
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    return {
        "observation": observation,
        "judgement": owner.judge(observation),
        "module": owner.module,
        "definition_digest": definition_digest(owner.definition),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = _run_one()
    Path(os.environ["SBH_TYPED_EVENT_PATH"]).write_text(
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
                        "type": "state-observation",
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
