#!/usr/bin/env python3
"""Interactive TUI rendering smoke test (pty, zero model budget).

Why it exists: headless (`-p`) and the interactive TUI are two independent
implementations. Testing only headless systematically misses render-layer bugs
where the logic is correct and the text is printed, but the terminal Live region
erases it and the user sees nothing. Classic case: printing the reply inside a
`rich console.status()` Live spinner context, where each spinner refresh clears
the line with `\r\x1b[2K` and wipes the reply -> blank after "Assistant:".

Must run under a real pty: without a tty, rich's Live is a no-op and does not
repaint, so the bug does not reproduce and the test would falsely pass.

Two complementary guards, both required:
  A) render_stub - stubs `_agentic_turn` to return a fixed sentinel, checking
     only that the render layer displays a non-empty reply. Isolates render bugs
     precisely, but bypasses the turn itself.
  B) e2e_reply  - no stub; points the real TUI at the mock gateway (known reply
     text) and runs the full `_agentic_turn` -> model -> render chain. Whether
     the reply is erased by rendering, or the turn returns empty/crashes, or the
     model is wired up wrong, B fails whenever the user cannot see the reply.

Usage: python tui_smoke.py <agent_dir> [--emit-json]   (needs the d2a interpreter that bundles
litellm)
"""
from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mock_gateway as MG  # noqa: E402

STUB_SENTINEL = "YUYAO_TUI_STUB_SENTINEL_4242"
E2E_SENTINEL = "YUYAO_TUI_E2E_REPLY_8841"
DONE = "__TUI_SMOKE_DONE__"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]|[\r\x07\x08]")
_RICH = re.compile(r"\[[^\]]*\]")
_REPLY_HEADERS = ("Assistant:", "Agent:")


def _plain(visible: str) -> str:
    return _RICH.sub("", visible)




# Adaptive bootstrap: discover and drive the interactive layer by behavioural
# contract, without pinning internal layout/class/method names (see state_smoke._BOOT).
# Rendering also needs the chat/turn method name resolved (chat / handle_message /
# agentic_turn vary by build); see _say / _TURN_NAMES.
_BOOT = '''
import importlib as _il, inspect as _ins
_DISP_NAMES = ("handle_command","handle_slash_command","dispatch_command","run_command","do_command","slash_command","on_command","handle_input")
_TURN_NAMES = ("_agentic_turn","agentic_turn","run_turn","turn","step","agent_loop","_turn","_run_turn")
_SAY_NAMES = ("chat","handle_message","process_message","process_user_message","send_message","ask","run_once","prompt_once")
def _find_cls(mods, pred):
    for _mn in mods:
        try: _m = _il.import_module(_mn)
        except Exception: continue
        for _nm, _c in _ins.getmembers(_m, _ins.isclass):
            try:
                if not getattr(_c, "__module__", "").startswith("yuyao"): continue
                if pred(_c): return _c
            except Exception: pass
    return None
_CfgCls = _find_cls(("yuyao.core.config","yuyao.config","yuyao.core.agent_config","yuyao.agent.config","yuyao.core.agent"),
                    lambda c: hasattr(c, "from_env"))
_TUICls = _find_cls(("yuyao.tui.main","yuyao.tui","yuyao.tui.app","yuyao.cli","yuyao.app","yuyao.core.tui","yuyao.interface","yuyao.main","yuyao.repl"),
                    lambda c: any(callable(getattr(c, _d, None)) for _d in _DISP_NAMES))
assert _TUICls is not None, "no TUI class with a slash-dispatch method found"
class _Args(object):
    def __init__(self, **kw): self.__dict__.update(kw)
    def __getattr__(self, n): return None
def _mk_tui(deny_rules=None):
    _dr = list(deny_rules or [])
    _cfg = None
    if _CfgCls is not None:
        for _ctor in (lambda: _CfgCls.from_env(), lambda: _CfgCls()):
            try: _cfg = _ctor(); break
            except Exception: _cfg = None
    _args = _Args(mode=None, role=None, max_steps=20, stream="off", model=None,
                  deny=_dr, deny_rules=_dr, no_memory=False, repo_dir=None, cwd=None, yolo=False)
    _att = []
    if _cfg is not None:
        _att.append(((_cfg,), {"deny_rules": _dr}))
        _att.append(((_cfg,), {}))
    _att.append(((_args,), {}))
    _att.append(((), {}))
    _last = None
    for _a, _k in _att:
        try:
            return _TUICls(*_a, **_k)
        except Exception as _e:
            _last = _e
    raise _last
def _set_stream(tui, on):
    for _a in ("stream_enabled","stream","streaming"):
        try: setattr(tui, _a, on)
        except Exception: pass
    try: tui.config.stream = on
    except Exception: pass
def _ensure_model(tui):
    if getattr(tui, "model", None) is not None: return
    _mc = getattr(tui, "model_config", None)
    if _mc is not None:
        for _bm in ("build_model", "create_model", "get_model"):
            _f = getattr(_mc, _bm, None)
            if callable(_f):
                try:
                    tui.model = _f(); return
                except Exception: pass
        if callable(getattr(_mc, "is_configured", None)) and _mc.is_configured():
            try:
                tui.model = _mc.build_model(); return
            except Exception: pass
    for _sm in ("setup_model","_setup_model","init_model","connect_model","_connect_model","setup"):
        _f = getattr(tui, _sm, None)
        if callable(_f):
            try: _f()
            except Exception: pass
            if getattr(tui, "model", None) is not None: return
def _wire_model_config(tui):
    # TUI constructor signatures vary between versions; always supply model_config from
    # D2A_MODEL_* so an e2e run cannot accidentally reach the default upstream (reqs=0).
    _cfg = {"model_name": os.environ.get("D2A_MODEL_NAME", ""),
            "model_kwargs": {"api_base": os.environ.get("D2A_MODEL_BASE_URL", ""),
                             "api_key": os.environ.get("D2A_MODEL_API_KEY", "sk-x")}}
    for _attr in ("model_config",):
        try:
            _v = getattr(tui, _attr, None)
            if isinstance(_v, dict):
                _v.update(_cfg); return
        except Exception: pass
    try: tui.model_config = _cfg
    except Exception: pass
def _ensure_executor(tui):
    if getattr(tui, "executor", None) is not None: return
    _wire_model_config(tui)
    for _nm in ("_ensure_executor", "_init_session", "_init_runtime", "_bootstrap"):
        _f = getattr(tui, _nm, None)
        if callable(_f):
            try: _f()
            except Exception: pass
            if getattr(tui, "executor", None) is not None: return
    try:
        from minisweagent.models import get_model
        from minisweagent.environments.local import LocalEnvironment
        _mc = getattr(tui, "model_config", None) or {}
        if not isinstance(_mc, dict): _mc = {}
        if getattr(tui, "model", None) is None:
            tui.model = get_model(config={"model_class": "litellm", **_mc})
        if getattr(tui, "env", None) is None:
            tui.env = LocalEnvironment(cwd=getattr(tui, "cwd", None) or os.getcwd())
        _Ex = None
        for _mn in ("yuyao.agent_wrapper", "yuyao.executor", "yuyao.core.executor"):
            try:
                _m = _il.import_module(_mn)
                _Ex = getattr(_m, "AgenticTurnExecutor", None) or getattr(_m, "TurnExecutor", None)
                if _Ex is not None: break
                for _cn, _c in _ins.getmembers(_m, _ins.isclass):
                    if "executor" in _cn.lower():
                        _Ex = _c; break
            except Exception: pass
            if _Ex is not None: break
        if _Ex is None: return
        _kw = {"max_steps": getattr(tui, "max_steps", 50), "mode": getattr(tui, "mode", "ask"),
               "role": getattr(tui, "role", "execute"), "auto_commit": getattr(tui, "auto_commit", False)}
        for _args in ((tui.model, tui.env), (tui.model, tui.env, _kw.get("max_steps"))):
            try:
                tui.executor = _Ex(*_args, **_kw); return
            except TypeError:
                try: tui.executor = _Ex(*_args); return
                except Exception: pass
            except Exception: pass
    except Exception: pass
def _stream_on(tui):
    for _a in ("stream_enabled", "stream", "streaming"):
        try:
            _v = getattr(tui, _a, None)
            if _v in (True, "on", "ON", 1): return True
        except Exception: pass
    return False
def _fake_executor(sentinel):
    class _Ex:
        messages = []
        def agentic_turn(self, *a, **k): return sentinel
        def agentic_turn_streaming(self, msg, console=None, **k):
            if console is not None:
                try: console.print(sentinel)
                except Exception:
                    import sys; sys.stdout.write(sentinel + "\\n"); sys.stdout.flush()
            else:
                import sys; sys.stdout.write(sentinel + "\\n"); sys.stdout.flush()
            return sentinel
    return _Ex()
def _stub_turn(tui, sentinel):
    # Stubbing must cover executor.agentic_turn*: many builders route chat/process_user_message
    # through the executor rather than TUI.turn.
    _fn = (lambda *a, **k: sentinel)
    def _render(msg, *a, **k):
        _c = getattr(tui, "console", None)
        if _c is not None:
            try: _c.print("Assistant: " + sentinel)
            except Exception: pass
        else:
            import sys; sys.stdout.write("Assistant: " + sentinel + "\\n"); sys.stdout.flush()
        return sentinel
    for _tn in _TURN_NAMES:
        try: setattr(tui, _tn, _fn)
        except Exception: pass
    for _pm in ("process_message", "process_user_message"):
        if callable(getattr(tui, _pm, None)):
            try: setattr(tui, _pm, _render)
            except Exception: pass
    _ex = getattr(tui, "executor", None)
    if _ex is None:
        tui.executor = _fake_executor(sentinel)
    else:
        try: _ex.agentic_turn = _fn
        except Exception: pass
        try:
            def _stream(msg, console=None, **k):
                if console is not None:
                    try: console.print(sentinel)
                    except Exception: pass
                return sentinel
            _ex.agentic_turn_streaming = _stream
        except Exception: pass
def _say(tui, text):
    _ensure_model(tui)
    _ensure_executor(tui)
    if _stream_on(tui):
        for _pm in ("process_user_message", "process_message"):
            _pmf = getattr(tui, _pm, None)
            if callable(_pmf):
                try: _pmf(text); return ""
                except Exception: pass
    for _nm in _SAY_NAMES:
        _f = getattr(tui, _nm, None)
        if callable(_f): return _f(text)
    try: tui.messages.append({"role":"user","content":text})
    except Exception: pass
    for _tn in _TURN_NAMES:
        _t = getattr(tui, _tn, None)
        if not callable(_t): continue
        # Turn-method signatures vary between versions: some take the user text as a positional
        # argument (e.g. run_turn(self, user)), others take none (the message is already in
        # tui.messages and the method reads it itself). Older code always called _t() with no
        # arguments, which raised a spurious TypeError (missing 'user') for the first kind.
        # Inspect the signature to decide whether a positional argument is needed; if that
        # inspection is inconclusive, try both arities.
        try:
            _sig = _ins.signature(_t)
            _need = any(
                (_p.kind in (_p.POSITIONAL_ONLY, _p.POSITIONAL_OR_KEYWORD) and _p.default is _p.empty)
                or _p.kind == _p.VAR_POSITIONAL
                for _p in _sig.parameters.values())
        except (TypeError, ValueError):
            _need = True
        try:
            return _t(text) if _need else _t()
        except TypeError:
            try: return _t(text)
            except TypeError: return _t()
    raise RuntimeError("no chat/turn method on TUI")
'''


def _preamble(agent_src: str, stream: bool, base_url: str, model: str) -> str:
    return (
        "import os,sys,json\n"
        # Put both `<agent>/src` and `<agent>` on the path: §0.1.2 only requires an
        # importable `yuyao.agent_api` module and says nothing about directory layout.
        # A flat layout (`yuyao/` at the project root) is one of setuptools' two
        # standard layouts, so inserting only `<agent>/src` would break every import
        # in such builds and fail them spuriously.
        f"sys.path.insert(0, {agent_src!r})\n"
        f"sys.path.insert(0, {os.path.dirname(agent_src)!r})\n"
        f"os.environ['D2A_MODEL_BASE_URL']={base_url!r}\n"
        f"os.environ['D2A_MODEL_NAME']={model!r}; os.environ['D2A_MODEL_API_KEY']='sk-x'\n"
        "os.environ['MSWEA_SILENT_STARTUP']='1'\n"
        + _BOOT +
        "tui = _mk_tui()\n"
        "_wire_model_config(tui)\n"
        f"_set_stream(tui, {stream})\n"
    )


def _child_stub(agent_src: str, stream: bool) -> str:
    # No model at all; stub the turn method so only the render layer is exercised.
    return (
        _preamble(agent_src, stream, "http://127.0.0.1:1", "m")
        + f"_stub_turn(tui, {STUB_SENTINEL!r})\n"
        + "_say(tui, 'hi')\n"
        + f"sys.stdout.write('\\n{DONE}\\n'); sys.stdout.flush()\n"
    )


def _child_e2e(agent_src: str, stream: bool, port: int) -> str:
    # Real chain: point at the mock gateway and run the real turn (no stub).
    return (
        _preamble(agent_src, stream, f"http://127.0.0.1:{port}", "litellm_proxy/mock")
        + "_say(tui, 'hi')\n"
        + f"sys.stdout.write('\\n{DONE}\\n'); sys.stdout.flush()\n"
    )


def _run_in_pty(agent_dir: Path, child_code: str, timeout: float = 30.0) -> str:
    """Run the given child code in a real pty; return the visible text, ANSI stripped."""
    import pty
    py = sys.executable
    master, slave = pty.openpty()
    workdir = tempfile.mkdtemp(prefix="tui_smoke_")
    env = dict(os.environ)
    env["COLUMNS"] = "80"; env["LINES"] = "24"; env["TERM"] = "xterm"
    env["HOME"] = workdir  # isolate ~/.yuyao; never touch the real home
    p = subprocess.Popen([py, "-c", child_code], stdin=slave, stdout=slave, stderr=slave,
                         cwd=workdir, env=env, close_fds=True)
    os.close(slave)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([master], [], [], 0.5)
        if r:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            buf += data
            if DONE.encode() in buf:
                break
        elif p.poll() is not None:
            try:
                while True:
                    data = os.read(master, 65536)
                    if not data:
                        break
                    buf += data
            except OSError:
                pass
            break
    try:
        os.close(master)
    except OSError:
        pass
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()
    return _ANSI.sub("", buf.decode("utf-8", errors="replace"))


def _check(visible: str, sentinel: str, label: str) -> dict:
    plain = _plain(visible)
    finished = DONE in visible
    seen = sentinel in plain       # reply text visible in the real render (not erased by the spinner)
    header = any(h in plain for h in _REPLY_HEADERS)
    ok = finished and seen
    why = f"reply visible in render={seen} reply header present={header} run completed={finished}"
    if header and not seen:
        why += " <- classic symptom: header rendered but the reply was erased (the 'mute agent' failure)"
    if not finished:
        why += f" | last 80 chars={visible[-80:]!r}"
    return {"id": label, "ok": ok, "blocking": True, "why": why}


def run_smoke(agent_dir: Path) -> list[dict]:
    results = []
    # A) render-isolation guard (stubbed turn): stream on/off
    for stream in (True, False):
        try:
            vis = _run_in_pty(agent_dir, _child_stub(str((agent_dir / "src").resolve()), stream))
            results.append(_check(vis, STUB_SENTINEL, f"render_stub_stream_{stream}"))
        except Exception as e:
            results.append({"id": f"render_stub_stream_{stream}", "ok": False, "blocking": True,
                            "why": f"harness error while running the case {type(e).__name__}: {e}"})
    # B) end-to-end guard (no stub, real mock gateway): stream on/off
    for stream in (True, False):
        srv = None
        try:
            cfg = MG.new_cfg(mode="plain_no_tool", reply_content=E2E_SENTINEL)
            srv = MG.make_server(cfg, 0)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            vis = _run_in_pty(agent_dir, _child_e2e(str((agent_dir / "src").resolve()), stream, port))
            res = _check(vis, E2E_SENTINEL, f"e2e_reply_stream_{stream}")
            res["why"] += f" reqs={len(cfg['requests'])}"
            results.append(res)
        except Exception as e:
            results.append({"id": f"e2e_reply_stream_{stream}", "ok": False, "blocking": True,
                            "why": f"harness error while running the case {type(e).__name__}: {e}"})
        finally:
            if srv is not None:
                srv.shutdown()
    return results


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir")
    ap.add_argument("--emit-json", action="store_true",
                    help="additionally print TUI_RESULTS=<json> + TUI_SMOKE_OK/FAIL for pipeline consumption")
    args = ap.parse_args()
    agent_dir = Path(args.agent_dir)
    if (agent_dir / "agent").is_dir():
        agent_dir = agent_dir / "agent"
    # Found if `yuyao/` sits under either `src/` or the project root: §0.1.2 does not
    # mandate a directory layout, so a flat layout is equally valid.
    if not any((agent_dir / c / "yuyao").is_dir() for c in ("src", ".")) \
            and not (agent_dir / "src").is_dir():
        print(f"[tui_smoke] agent src not found: {agent_dir}", file=sys.stderr)
        if args.emit_json:
            print('TUI_RESULTS=[{"id": "_tui_harness", "ok": false, "blocking": true, "why": "no agent src"}]')
            print("TUI_SMOKE_FAIL")
        sys.exit(2)
    print(f"[tui_smoke] agent: {agent_dir}")
    results = run_smoke(agent_dir)
    npass = sum(r["ok"] for r in results)
    for r in results:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['id']:24s} {r['why']}")
    print(f"== TUI render smoke: {npass}/{len(results)} passed ==")
    import json as _json
    blocking_ok = all(r["ok"] for r in results if r.get("blocking"))
    if args.emit_json:
        print("TUI_RESULTS=" + _json.dumps(results, ensure_ascii=False))
        print("TUI_SMOKE_OK" if blocking_ok else "TUI_SMOKE_FAIL")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
