"""State/sequence determinism smoke (pure slash commands, zero model budget).

Why it exists: `config` / `mcp` / `init` / `export` / memory slash commands NEED NO MODEL AT ALL -- they only
change local state (the persistent_config file / the `mcp_servers` list / AGENTS.md / MEMORY.md). So the
order-sensitivity and idempotence/repetition axes of the "command permutation" matrix can be decided FULLY
DETERMINISTICALLY WITH ZERO BUDGET: construct the TUI, run `handle_command` in order, then read the observable
state. The result is constant and immune to real-model nondeterminism.

Division of labour with perm_smoke: perm_smoke tests "the permission layer fails closed" (it needs a mock to
drive a forbidden tool_call); state_smoke tests "the order/idempotence correctness of purely local state
commands" (it does not even need a mock).

Verdict anchors (each command carries its own adjudication, collapsed into state facts; the child self-reports
STATE_RESULT so no ANSI parsing is needed):
  · config_* : read persistent_config.get(key); check the set/get round trip and later-write-wins.
  · mcp_*    : read the tui.mcp_servers list; check add/remove ordering and idempotence.
  · init_*   : read AGENTS.md; check it does not clobber existing content and does not write twice.
  · memory_* : read MEMORY.md; check forget deletes only the matched entry and the rest survive.

Dependency: only the ability to import the agent's yuyao package (no litellm call needed).
Usage: python state_smoke.py <agent_dir> [--emit-json]
"""

from __future__ import annotations
import atexit
import json
import os
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import mock_gateway as MG
import smoke_contract as SC

RESULT_TAG = "STATE_RESULT="
_DEFAULT_STUB = {
    "mode": "plain_no_tool",
    "reply_content": "Summary: task context compacted. STUB_SUMMARY_5150",
}




_BOOT = '''
import importlib as _il, inspect as _ins
_DISP_NAMES = ("handle_command","handle_slash_command","dispatch_command","run_command","do_command","slash_command","on_command","handle_input","dispatch_slash","slash","handle_slash","execute_slash","process_slash")
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
# Standard programmatic interface (spec §0.1.2 / AGENT_CONTRACT §7): prefer driving through the fixed contract
#   yuyao.agent_api.create_session; if absent, fall back to the adaptive bootstrap (old artifacts can still be
#   rejudged, zero regression).
def _find_std():
    for _mn in ("yuyao.agent_api","yuyao.d2a_api","yuyao.harness_api","agent_api"):
        try: _m = _il.import_module(_mn)
        except Exception: continue
        _f = getattr(_m, "create_session", None)
        if callable(_f): return _f
    return None
_STD_FACTORY = _find_std()
assert _STD_FACTORY is not None or _TUICls is not None, "no standard yuyao.agent_api and no TUI class with a slash-dispatch method found"
def _mk_std(deny_rules=None, mode=None, role=None):
    if _STD_FACTORY is None: return None
    _kw = {}
    if deny_rules: _kw["deny"] = list(deny_rules)
    if mode: _kw["mode"] = mode
    if role: _kw["role"] = role
    _s = None
    for _try in (lambda: _STD_FACTORY(**_kw), lambda: _STD_FACTORY()):
        try: _s = _try(); break
        except Exception: _s = None
    if _s is None: return None
    try: _s._d2a_std = True
    except Exception: pass
    return _s
class _Args(object):
    def __init__(self, **kw): self.__dict__.update(kw)
    def __getattr__(self, n): return None
def _mk_tui(deny_rules=None):
    _dr = list(deny_rules or [])
    # Standard contract first.
    _std = _mk_std(deny_rules=_dr)
    if _std is not None:
        return _std
    # model_config DICT style (e.g. YuyaoTUI(model_config: dict, mode, role, ...)): assembled from D2A_MODEL_*.
    # Otherwise config-style commands (/config, /mcp ... still purely local) would run, but constructing commands
    # that need the model would fail; feed the dict style uniformly.
    _mcfg = {"model_name": os.environ.get("D2A_MODEL_NAME", ""),
             "model_kwargs": {"api_base": os.environ.get("D2A_MODEL_BASE_URL", ""),
                              "api_key": os.environ.get("D2A_MODEL_API_KEY", "")}}
    _cfg = None
    if _CfgCls is not None:
        for _ctor in (lambda: _CfgCls.from_env(), lambda: _CfgCls()):
            try: _cfg = _ctor(); break
            except Exception: _cfg = None
    _args = _Args(mode=None, role=None, max_steps=20, stream="off", model=None,
                  deny=_dr, deny_rules=_dr, no_memory=False, repo_dir=None, cwd=None, yolo=False)
    _pnames = []
    try:
        _pnames = [_p.name for _p in _ins.signature(_TUICls.__init__).parameters.values() if _p.name != "self"]
    except Exception: pass
    _first = _pnames[0] if _pnames else ""
    _dict_first = _first in ("model_config", "model_cfg", "config_dict", "mcfg")
    _att = []
    if _dict_first or "model_config" in _pnames:
        _att.append(((_mcfg,), {}))
        _att.append(((), {"model_config": _mcfg}))
    # Keyword-only construction style (e.g. YuyaoTUI(*, model_name, api_base, api_key, deny_rules, ...)):
    # feed D2A_MODEL_* + deny_rules using the parameter names that REALLY EXIST in the signature, avoiding
    # positional arguments failing against keyword-only (driving only; assertions untouched).
    _sig_kw = {}
    for _kn, _kv in (("model_name", os.environ.get("D2A_MODEL_NAME","")),
                     ("api_base", os.environ.get("D2A_MODEL_BASE_URL","")),
                     ("api_key", os.environ.get("D2A_MODEL_API_KEY",""))):
        if _kn in _pnames:
            _sig_kw[_kn] = _kv
    if _sig_kw:
        if _dr and "deny_rules" in _pnames:
            _att.append(((), dict(_sig_kw, deny_rules=_dr)))
        if _dr and "deny" in _pnames:
            _att.append(((), dict(_sig_kw, deny=_dr)))
        _att.append(((), dict(_sig_kw)))
    # Scalar-first signature (e.g. YuyaoTUI(mode, role, max_steps, ...), model via env, no model_config/model_name
    # parameter): construction MUST be keyword-only and must never pass _mcfg/_args as the first positional --
    # otherwise the dict/_Args lands on the `mode` parameter, self.mode becomes a dict, and every `{..}.get(self.mode)`
    # site (the system prompt, some slashes such as /clear and /new) raises TypeError: unhashable type 'dict'
    # -> a false negative (really a harness construction blind spot, not a build bug). Highest priority.
    _scalar_first = _first in ("mode", "role", "max_steps", "stream", "model", "cwd",
                               "no_memory", "auto_commit", "verbose", "show_thinking")
    if _scalar_first and "model_config" not in _pnames and not _sig_kw:
        _safe = {}
        if _dr and "deny" in _pnames: _safe["deny"] = _dr
        if _dr and "deny_rules" in _pnames: _safe["deny_rules"] = _dr
        _att.insert(0, ((), _safe))
    if _cfg is not None:
        _att.append(((_cfg,), {"deny_rules": _dr}))
        _att.append(((_cfg,), {}))
    _att.append(((_mcfg,), {}))  # fallback: dict style
    _att.append(((_args,), {}))
    _att.append(((), {}))
    _last = None
    for _a, _k in _att:
        try:
            _t = _TUICls(*_a, **_k)
            if _dr:
                for _dn in ("deny_rules","deny"):
                    if not getattr(_t, _dn, None):
                        try: setattr(_t, _dn, _dr)
                        except Exception: pass
                _ag = getattr(_t, "args", None)
                if _ag is not None and not getattr(_ag, "deny", None):
                    try: _ag.deny = _dr
                    except Exception: pass
            return _t
        except Exception as _e:
            _last = _e
    raise _last
def _dispatch(tui, c):
    if getattr(tui, "_d2a_std", False) and callable(getattr(tui, "dispatch", None)):
        return tui.dispatch(c)  # standard contract: go straight through AgentSession.dispatch
    for _d in _DISP_NAMES:
        _f = getattr(tui, _d, None)
        if callable(_f): return _f(c)
    raise RuntimeError("no slash dispatch method on TUI")
def _ensure_model(tui):
    if getattr(tui, "model", None) is not None: return
    for _sm in ("setup_model","_setup_model","init_model","connect_model","_connect_model","setup"):
        _f = getattr(tui, _sm, None)
        if callable(_f):
            try: _f()
            except Exception: pass
            if getattr(tui, "model", None) is not None: return
def _chat(tui, text, allow_synthetic=False):
    # allow_synthetic defaults off: the last resort, "messages.append(user) + agentic_turn()", is a SYNTHETIC
    # INJECTION -- it bypasses the implementation's own user-message enqueueing logic, i.e. the harness doing
    # the agent's work, manufacturing a vacuum PASS.
    # Only cases that explicitly want history built up regardless may turn it on.
    if getattr(tui, "_d2a_std", False) and callable(getattr(tui, "chat", None)):
        return tui.chat(text)  # standard contract: go straight through AgentSession.chat
    _ensure_model(tui)
    for _nm in ("chat","process_input","handle_message","process_message","send_message","handle_user_input","ask","run_once","prompt_once"):
        _f = getattr(tui, _nm, None)
        if callable(_f): return _f(text)
    if not allow_synthetic:
        raise RuntimeError("no chat method on TUI (synthetic injection disabled)")
    try: tui.messages.append({"role":"user","content":text})
    except Exception: pass
    for _tn in ("agentic_turn","run_turn","turn","step","agent_loop","_turn"):
        _t = getattr(tui, _tn, None)
        if callable(_t): return _t()
    raise RuntimeError("no chat/turn method on TUI")
def _read_cfg(tui, k):
    _v = None
    # Standard contract: if the session exposes config_get(key), prefer it (a deterministic read of the current config value).
    if getattr(tui, "_d2a_std", False) and callable(getattr(tui, "config_get", None)):
        try:
            _v = tui.config_get(k)
        except Exception:
            _v = None
        if _v not in (None, ""):
            return _v
    # (1) Prefer the agent's own config accessors (persistent_config / config_manager / config) without pinning the
    #     storage path -- lesson learned: the harness used to hardcode ~/.yuyao/config.json while a build wrote to
    #     ~/.config/yuyao/config.json, giving false negatives on every config case.
    for _an in ("persistent_config", "config_manager", "config", "_config", "settings", "_settings"):
        _pc = getattr(tui, _an, None)
        if _pc is not None and callable(getattr(_pc, "get", None)):
            try:
                _v = _pc.get(k)
            except Exception:
                _v = None
            if _v not in (None, ""):
                return _v
    # (2) On-disk fallback: search both the spec path ~/.yuyao and the common implementation ~/.config/yuyao.
    if _v in (None, ""):
        for _p in (os.path.join(os.environ.get("HOME",""), ".yuyao", "config.json"),
                   os.path.join(os.environ.get("HOME",""), ".config", "yuyao", "config.json")):
            try:
                if os.path.exists(_p):
                    _d = json.load(open(_p, encoding="utf-8"))
                    if k in _d:
                        return _d[k]
            except Exception: pass
    return _v
def _ensure_executor(tui):
    """Trigger the agent's own lazy executor initialisation (without pinning internal construction details).
    Many builds keep session history in tui.executor.messages rather than tui.messages, and the executor is only
    built by the first command that needs it.
    A set of "benign read-only commands that need an executor" is tried in turn and whichever builds tui.executor
    first is used; messages and disk are never touched."""
    if getattr(tui, "executor", None) is not None:
        return tui.executor
    for _c in ("/diff", "/status", "/recap", "/context list", "/permissions", "/usage", "/cd ."):
        try: _dispatch(tui, _c)
        except Exception: pass
        if getattr(tui, "executor", None) is not None:
            return tui.executor
    return getattr(tui, "executor", None)
def _conv_list(tui):
    """The real list holding session history: prefer executor.messages (most builds), otherwise tui.messages."""
    if getattr(tui, "_d2a_std", False):
        _m = getattr(tui, "messages", None)  # standard contract: .messages IS the active session list
        if isinstance(_m, list):
            return _m
    _exe = getattr(tui, "executor", None)
    if _exe is not None and isinstance(getattr(_exe, "messages", None), list):
        return _exe.messages
    # Delegate to inner objects: some builds keep session history on tui.agent/session/engine (e.g. tui.agent.messages).
    for _an in ("agent", "_agent", "session", "_session", "engine", "core"):
        _o = getattr(tui, _an, None)
        if _o is not None and isinstance(getattr(_o, "messages", None), list):
            return _o.messages
    return getattr(tui, "messages", None)
def _read_msgs(tui):
    _lst = _conv_list(tui)
    if _lst is None:
        return None, -1, None
    try:
        _blob = "\\n".join(str(m.get("role")) + ":" + str(m.get("content")) for m in _lst)
        # Per message: (role, has tool_calls, has tool_call_id) -- used to verify the message sequence still
        # satisfies the OpenAI/litellm constraints after a compact/rewind slice (a tool must immediately follow an
        # assistant carrying tool_calls; an assistant.tool_calls must have a tool response).
        _roles = []
        for _m in _lst:
            if isinstance(_m, dict):
                _roles.append([_m.get("role"), bool(_m.get("tool_calls")), bool(_m.get("tool_call_id"))])
            else:
                _roles.append([None, False, False])
        return _blob, len(_lst), _roles
    except Exception:
        return None, -1, None
def _read_mcp(tui):
    # Standard contract: if the session exposes mcp_servers (list[str]), prefer it.
    if getattr(tui, "_d2a_std", False):
        _ms = getattr(tui, "mcp_servers", None)
        if isinstance(_ms, (list, tuple)):
            return list(_ms)
    # Do not pin internal attribute names: try mcp_servers / _mcp_servers / mcp in turn. Elements may be str or dict (take its name/id/server).
    for _an in ("mcp_servers", "_mcp_servers", "mcp"):
        _ms = getattr(tui, _an, None)
        if isinstance(_ms, (list, tuple)):
            _out = []
            for _e in _ms:
                if isinstance(_e, str):
                    _out.append(_e)
                elif isinstance(_e, dict):
                    _out.append(_e.get("name") or _e.get("id") or _e.get("server") or _e)
                else:
                    _out.append(getattr(_e, "name", None) or getattr(_e, "id", None) or _e)
            return _out
    try:
        _p = os.path.join(os.environ.get("HOME",""), ".yuyao", "mcp.json")
        if os.path.exists(_p):
            return json.load(open(_p, encoding="utf-8"))
    except Exception: pass
    return None
def _probe_std(tui, probe_present_value=False):
    """The v6§0.1.2 member surface: only whether THE STANDARD SESSION OBJECT has these members and their types are right.

    Why a separate channel instead of reusing _read_cfg / _read_mcp / _read_msgs: those three readers FALL BACK
      to the agent's internal attributes (persistent_config / _mcp_servers / executor.messages) or even an
      on-disk config.json when a standard member is missing. The fallback exists so verdicts do not pin internal
      implementations and is right for the vast majority of cases; but when judging "does this §0.1.2 member
      exist", an artifact with no config_get at all would still get a value via the fallback and pass vacuously.
      This function always reads directly with no fallback, exclusively for the §0.1.2 member cases.

    It self-reports as {'std': True, 'cwd': {'present': True, 'type_ok': True}, ...}.
    When `std=False` (the artifact has no yuyao.agent_api.create_session) the other keys are left unfilled and
    the verdict scores inconclusive -- that bill is settled by the §0.1.1 "expose the standard programmatic
    interface" slot; this family does not penalise it again.
    """
    _o = {"std": bool(getattr(tui, "_d2a_std", False))}
    if not _o["std"]:
        return _o
    _c = getattr(tui, "cwd", None)
    _o["cwd"] = {"present": _c is not None, "type_ok": isinstance(_c, str)}
    _m = getattr(tui, "messages", None)
    _o["messages"] = {"present": _m is not None,
                      "type_ok": isinstance(_m, list) and all(isinstance(_x, dict) for _x in _m)}
    for _n in ("dispatch", "chat", "config_get", "close"):
        _f = getattr(tui, _n, None)
        _o[_n] = {"present": _f is not None, "type_ok": callable(_f)}
    _s = getattr(tui, "mcp_servers", None)
    _o["mcp_servers"] = {"present": _s is not None,
                         "type_ok": isinstance(_s, list)
                                    and all(isinstance(_x, str) for _x in _s)}
    # config_get must return None for A KEY THAT WAS NEVER SET and must not raise KeyError (v6§0.1.2 "None if unset").
    # Probed with a key that cannot ever have been set. If config_get is unavailable, this item is left unfilled.
    if _o["config_get"]["type_ok"]:
        if probe_present_value and _o["dispatch"]["type_ok"]:
            try:
                _dispatch(tui, "/config set model litellm_proxy/PRESENT_9931")
                _pv = tui.config_get("model")
                _o["config_get"]["present_roundtrip"] = "PRESENT_9931" in str(_pv)
                _o["config_get"]["present_value"] = str(_pv)[:120]
            except Exception as _e:
                _o["config_get"]["present_roundtrip"] = False
                _o["config_get"]["present_value"] = "raised:" + type(_e).__name__
        try:
            _v = tui.config_get("d2a_never_set_key_9931")
            _o["config_get"]["unset_returns"] = "None" if _v is None else ("empty" if _v == "" else "value")
        except Exception as _e:
            _o["config_get"]["unset_returns"] = "raised:" + type(_e).__name__
    return _o
'''
_HELP_PROBE_SKIP = {"/exit", "/quit", "/clear", "/reset", "/new"}
_PLACEHOLDER_TOKENS = {
    "/command",
    "/cmd",
    "/name",
    "/path",
    "/file",
    "/text",
    "/prompt",
    "/query",
    "/instruction",
    "/condition",
    "/level",
    "/args",
    "/arg",
    "/value",
    "/key",
    "/dir",
    "/url",
    "/n",
    "/x",
}


def _child_code(
    agent_src: str,
    cmds,
    cfg_keys,
    files,
    seed_files,
    premsgs=None,
    ensure_exec=False,
    git_init=False,
    post_files=None,
    deny=None,
    snap_after=None,
    cfg_snap_after=None,
    tree_suffixes=None,
    probe_help=False,
    ctor_kwargs=None,
    ctor_contrast=None,
    git_branch_commit=False,
    probe_std_present=False,
    settle_after=None,
    capture_command_times=False,
    home_seed=None,
) -> str:
    """Build the child script: seed files as needed, construct the TUI (no model required), optionally inject preset messages, run the slash commands in order, and self-report state facts.

    premsgs: messages injected into THE REAL SESSION LIST (executor.messages preferred, else tui.messages) after
    the TUI is constructed and before the commands run -- for testing commands that touch session history such as
    /compact, /clear, /new and /export. Injection uses append (preserving system[0]) and _ensure_executor runs
    first so the executor is built with the seeded AGENTS.md/MEMORY.md. res self-reports the messages text (msgs)
    and count (nmsgs), both from that real list.

    ensure_exec: force the executor to be built first even without premsgs (for cases like /clear that check
    whether the system constraints survive a clear).

    git_init: `git init` in the workdir and commit the already-seeded files as a baseline (for git-based commands
    such as /diff and /undo).
    post_files: files written AFTER the baseline commit (= untracked/uncommitted "agent changes"), used to check
    whether /diff can show a new file and whether /undo can remove it. The child also captures console output
    during the commands into res['console'] (/diff only prints without changing state, so its output must be observed).

    snap_after (default None = no snapshot, additive and inert): after running command N (0-based), snapshot
    `files` into `res['files_mid']`. Why it is needed: some requirements are "A PARTICULAR SEGMENT of commands
    must not change this file", and looking only at the final state cannot tell which segment wrote it, so the
    verdict is forced to recognise the file content's FORMAT (counting entries, finding fixed text) -- and the
    format is each build's own choice, so recognising the wrong one misjudges (counting entries by "lines
    starting with `-`" scores a build that writes `## memory-flush <ISO>` as 0 entries).
    With a mid snapshot the verdict can simply ask "did the file change over this segment", independent of format,
    and it incidentally catches OVERWRITING writes (looking only at the final state cannot distinguish overwrite
    from never-written).

    probe_help (default False = inert): DYNAMIC bidirectional probing. After `cmds` have run and all ordinary
    observations are collected, the commands `/help` CLAIMS to support are extracted from its output, dispatched
    one by one, and the replies collected into `res['help_probe']`.
    Why it must be dynamic: §8.6 compares "the command set this build claims" against "the set it actually
    supports", and the claimed side differs per build (each registry differs), so a static `cmds` list cannot
    test this at all.

    settle_after: only for genuinely backgrounded slash commands, giving the daemon child a bounded startup window
    after the named command index. Defaults to an empty mapping and changes no existing case; the wait happens
    after the product's dispatch returns and calls no private API on the product's behalf.
    Besides a float (a fixed sleep in seconds) it also accepts a dict {"until_token","deadline_s","poll_s"}:
    poll the gateway's /__d2a_seen introspection route until that probe token lands in the ledger or the deadline passes.

    capture_command_times: default False. Enabled only for the MCP generation lifecycle variants, recording
    monotonic_ns before dispatch and after return for each public command; it shares a clock with the stdio peer's
    own monotonic_ns and is used to adjudicate strictly whether the old peer still receives new wire after a
    generation is committed. When off, the result field is None and existing cases are unchanged.

    home_seed: only for the convention fixtures that must distinguish workspace from user fallback. Relative paths
    are written into the isolated HOME; combined with `_run_child(separate_home=True)` it prevents the old
    fixtures' cwd==HOME layout from collapsing the two priority levels into one directory. Defaults to an empty
    mapping; existing cases are bit-identical.

    ctor_contrast (default None = not executed): the BEHAVIOURAL CONTRAST channel for mode/role "passing it in
    takes effect". {"kwarg","blocked","allowed","prompt","blocked_file","allowed_file"} -- call
    `_STD_FACTORY(**{kwarg: level})` directly to build one session per level and drive one stub chat turn each (the
    mock returns the same write tool call, writing a sentinel file into the session cwd), then self-report whether
    each side's sentinel landed into res['contrast']. The verdict adjudicates from that: the blocked side did not
    land AND the allowed side did => the level really constrains behaviour.
    """
    _need_exec = bool(premsgs) or bool(ensure_exec)
    return (
        f'''import os,sys,json,traceback
sys.path.insert(0, {agent_src!r})
sys.path.insert(0, {os.path.dirname(agent_src)!r})
os.environ.setdefault('D2A_MODEL_BASE_URL','http://127.0.0.1:1')
os.environ.setdefault('D2A_MODEL_NAME','litellm_proxy/mock')
os.environ.setdefault('D2A_MODEL_API_KEY','sk-x')
os.environ['MSWEA_SILENT_STARTUP']='1'
_seed = json.loads({json.dumps(json.dumps(seed_files))})
for fn,c in _seed.items():
    _d = os.path.dirname(fn)
    if _d:
        os.makedirs(_d, exist_ok=True)
    open(fn,'w',encoding='utf-8').write(c)
_home_seed = json.loads({json.dumps(json.dumps(home_seed or {}))})
for fn,c in _home_seed.items():
    _p = os.path.join(os.environ.get('HOME',''), fn)
    _d = os.path.dirname(_p)
    if _d:
        os.makedirs(_d, exist_ok=True)
    open(_p,'w',encoding='utf-8').write(c)
'''
        + _BOOT
        + f'''_deny = json.loads({json.dumps(json.dumps(deny or []))})
_ck = json.loads({json.dumps(json.dumps(ctor_kwargs or {}))})
_ctor = None
if _ck:
    _ctor = {{}}
    try:
        _cs = _STD_FACTORY(**_ck) if _STD_FACTORY is not None else None
        _ctor['raised'] = ''
        _ctor['built'] = _cs is not None
        _ctor['cwd'] = str(getattr(_cs, 'cwd', '?') or '?')
        try:
            _cs.close()
        except Exception:
            pass
    except Exception as _e:
        _ctor['raised'] = type(_e).__name__ + ': ' + str(_e)[:160]
        _ctor['built'] = False
_cco = json.loads({json.dumps(json.dumps(ctor_contrast or {}))})
_contrast = None
if _cco and _STD_FACTORY is not None:
    _contrast = {{'kwarg': _cco.get('kwarg')}}
    for _side in ('blocked', 'allowed'):
        _fact = {{'value': _cco.get(_side), 'raised': None,
                 'wrote': None, 'chat_error': None}}
        _cs2 = None
        try:
            _kw2 = dict(_cco.get('extra') or {{}})
            _kw2[_cco['kwarg']] = _cco[_side]
            _cs2 = _STD_FACTORY(**_kw2)
            _fact['raised'] = ''
        except Exception as _ce:
            _fact['raised'] = type(_ce).__name__ + ': ' + str(_ce)[:160]
        if _cs2 is not None:
            try:
                _sd = str(getattr(_cs2, 'cwd', None) or os.getcwd())
                _fp = os.path.join(_sd, _cco[_side + '_file'])
                _cs2.chat(_cco['prompt'])
                _fact['wrote'] = os.path.exists(_fp)
            except Exception as _ce2:
                _fact['chat_error'] = type(_ce2).__name__ + ': ' + str(_ce2)[:160]
        try:
            if _cs2 is not None: _cs2.close()
        except Exception:
            pass
        _contrast[_side] = _fact
tui = _mk_tui(deny_rules=_deny)
_need_exec = {_need_exec!r}
if _need_exec:
    _ensure_executor(tui)
_pre = json.loads({json.dumps(json.dumps(premsgs or []))})
if _pre:
    _cl = _conv_list(tui)
    try:
        _cl.extend(_pre)
    except Exception:
        pass
_msgs_before, _nb, _roles_before = _read_msgs(tui)
_gi = {git_init!r}
if _gi:
    import subprocess as _sp
    _gc = dict(GIT_DIR='', GIT_WORK_TREE='')
    _sp.run('git init -q', shell=True, cwd=os.getcwd())
    _sp.run('git -c user.email=t@t.t -c user.name=t add -A', shell=True, cwd=os.getcwd())
    _sp.run('git -c user.email=t@t.t -c user.name=t commit -q -m base --allow-empty', shell=True, cwd=os.getcwd())
_post = json.loads({json.dumps(json.dumps(post_files or {}))})
for _fn,_c in _post.items():
    _dd = os.path.dirname(_fn)
    if _dd:
        os.makedirs(_dd, exist_ok=True)
    open(_fn,'w',encoding='utf-8').write(_c)
_gbc = {bool(git_branch_commit)!r}
if _gbc and _gi:
    import subprocess as _sp2
    _G = 'git -c user.email=t@t.t -c user.name=t '
    _sp2.run(_G + 'branch -M main', shell=True, cwd=os.getcwd(),
             stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
    _sp2.run(_G + 'checkout -q -b feat_9601', shell=True, cwd=os.getcwd(),
             stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
    _sp2.run(_G + 'add -A', shell=True, cwd=os.getcwd(),
             stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
    _sp2.run(_G + 'commit -q -m branchchange', shell=True, cwd=os.getcwd(),
             stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
    _sp2.run(_G + 'update-ref refs/remotes/origin/main refs/heads/main',
             shell=True, cwd=os.getcwd(), stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
    _sp2.run(_G + 'symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main',
             shell=True, cwd=os.getcwd(), stdout=_sp2.DEVNULL, stderr=_sp2.DEVNULL)
import io as _io
from contextlib import redirect_stdout as _rso
_cap = _io.StringIO()
if _gi:
    try:
        from rich.console import Console as _RC
        _con = _RC(file=_cap, force_terminal=False, no_color=True, width=200)
        tui.console = _con
        _exe2 = getattr(tui,'executor',None)
        if _exe2 is not None and hasattr(_exe2,'console'):
            _exe2.console = _con
    except Exception:
        pass
err=''
_rets = []
def _rd(n, _cwd=None):
    _cwd = _cwd or str(tui.cwd)
    p=os.path.join(_cwd,n)
    if '*' in n:
        import glob as _g
        _ms = sorted(_g.glob(p), key=lambda x: os.path.getmtime(x), reverse=True)
        p = _ms[0] if _ms else p
    return open(p,encoding='utf-8',errors='replace').read() if os.path.exists(p) else None
def _tree(_sfx, _cap_n=40, _cap_b=20000):
    _out = {{}}
    _root = os.getcwd()
    for _dp, _dn, _fn in os.walk(_root):
        _dn[:] = [d for d in _dn if d not in ('__pycache__', '.git')]
        for _f in _fn:
            if not any(_f.endswith(_s) for _s in _sfx):
                continue
            _p = os.path.join(_dp, _f)
            try:
                if os.path.getsize(_p) > _cap_b:
                    continue
                _out[os.path.relpath(_p, _root)] = open(_p,encoding='utf-8',errors='replace').read()
            except Exception:
                pass
            if len(_out) >= _cap_n:
                return _out
    return _out
_tree_suffixes = {tree_suffixes!r}
_fnames = json.loads({json.dumps(json.dumps(files))})
_snap_after = {snap_after!r}
_files_mid = None
_cfg_snap_after = {cfg_snap_after!r}
_cfg_keys = json.loads({json.dumps(json.dumps(cfg_keys))})
_cfg_mid = None
_cmds = json.loads({json.dumps(json.dumps(cmds))})
_settle_after = json.loads({json.dumps(json.dumps(settle_after or {}))})
_capture_command_times = {bool(capture_command_times)!r}
_command_times = []
with _rso(_cap):
    for _i, _c in enumerate(_cmds):
        _cmd_started_ns = (__import__('time').monotonic_ns()
                           if _capture_command_times else None)
        try:
            if str(_c).startswith('/'):
                _rets.append(str(_dispatch(tui, _c) or ''))
            else:
                try:
                    _rets.append(str(_chat(tui, _c) or ''))
                except Exception as _ce:
                    err += 'CHAT_FALLBACK %r raised %s\\n' % (_c, type(_ce).__name__)
                    _rets.append(str(_dispatch(tui, _c) or ''))
        except Exception as _e:
            err += 'CMD_ERR %r: %r\\n' % (_c, _e)
        _cmd_returned_ns = (__import__('time').monotonic_ns()
                            if _capture_command_times else None)
        _settle = _settle_after.get(str(_i), 0) or 0
        if isinstance(_settle, dict):
            _seen = False
            _dl = __import__('time').monotonic() + float(_settle.get('deadline_s') or 5.0)
            _poll = float(_settle.get('poll_s') or 0.05)
            _seen_url = (os.environ.get('D2A_MODEL_BASE_URL', '')
                         + '/__d2a_seen?token=' + str(_settle.get('until_token') or ''))
            while not _seen and __import__('time').monotonic() < _dl:
                try:
                    _sr = __import__('urllib.request', fromlist=['urlopen']).urlopen(_seen_url, timeout=5)
                    _seen = bool(json.loads(_sr.read().decode('utf-8')).get('seen'))
                except Exception:
                    _seen = False
                if not _seen:
                    __import__('time').sleep(_poll)
        else:
            _settle = float(_settle)
            if _settle > 0:
                __import__('time').sleep(_settle)
        if _capture_command_times:
            _command_times.append({{'index': _i, 'command': str(_c),
                                   'started_ns': _cmd_started_ns,
                                   'returned_ns': _cmd_returned_ns}})
        if _snap_after is not None and _i == _snap_after:
            try:
                _files_mid = {{n: _rd(n) for n in _fnames}}
            except Exception:
                _files_mid = None
        if _cfg_snap_after is not None and _i == _cfg_snap_after:
            try:
                _cfg_mid = {{k: _read_cfg(tui, k) for k in _cfg_keys}}
            except Exception:
                _cfg_mid = None
console_txt = _cap.getvalue()[-8000:]
cwd = str(tui.cwd)
cfg = {{k: _read_cfg(tui, k) for k in json.loads({json.dumps(json.dumps(cfg_keys))})}}
mcp = _read_mcp(tui)
files = {{n: _rd(n, cwd) for n in _fnames}}
_msgs, _nm, _roles = _read_msgs(tui)
def _mname(_m):
    if _m is None:
        return ''
    _v = getattr(_m, 'model_name', None)
    if _v:
        return str(_v)
    _c = getattr(_m, 'config', None)
    if _c is not None:
        _v = getattr(_c, 'model_name', None) or (isinstance(_c, dict) and _c.get('model_name'))
        if _v:
            return str(_v)
    return str(_c if _c is not None else _m)
try:
    _mt = _mname(getattr(tui, 'model', None))
except Exception:
    _mt = ''
try:
    _eo = getattr(tui, 'executor', None); _me = _mname(getattr(_eo, 'model', None)) if _eo is not None else ''
except Exception:
    _me = ''
try:
    _mode_tui = str(getattr(tui, 'mode', None))
except Exception:
    _mode_tui = None
try:
    _plans = sorted(os.listdir(os.path.join(cwd, '.plans')))
except Exception:
    _plans = None
_probe_help = {probe_help!r}
_help_probe = {{}}
_help_claimed = []
if _probe_help:
    import re as _re2
    _hseg = ' || '.join(_rets) + ' || ' + console_txt
    _help_claimed = sorted(set(_re2.findall(r'/[a-z][a-z0-9:-]{{1,}}', _hseg)))
    _hskip = set(json.loads({json.dumps(json.dumps(sorted(_HELP_PROBE_SKIP | _PLACEHOLDER_TOKENS)))}))
    _sink = _io.StringIO()
    with _rso(_sink):
        for _hc in [c for c in _help_claimed if c not in _hskip][:60]:
            try:
                _help_probe[_hc] = str(_dispatch(tui, _hc) or '')[:200]
            except BaseException as _he:
                _help_probe[_hc] = 'EXC:%s: %s' % (type(_he).__name__, _he)
res = {{'ctor': _ctor,
       'contrast': _contrast,
       'cfg': cfg, 'cfg_mid': _cfg_mid, 'mcp': mcp, 'files': files, 'files_mid': _files_mid,
       'help_probe': _help_probe, 'help_claimed': _help_claimed,
       'plans': _plans,
       'tree': (_tree(_tree_suffixes) if _tree_suffixes else None),
       'msgs': _msgs, 'nmsgs': _nm,
       'msgs_before': _msgs_before, 'roles': _roles, 'roles_before': _roles_before,
       'console': console_txt, 'mdl_tui': _mt, 'mdl_exec': _me, 'mode_tui': _mode_tui,
       'command_times': (_command_times if _capture_command_times else None),
       'std': _probe_std(tui, {bool(probe_std_present)!r}),
       'returns': _rets, 'cwd': cwd, 'err': err}}
sys.stdout.write('\\n' + {RESULT_TAG!r} + json.dumps(res) + '\\n'); sys.stdout.flush()
'''
    )


def _run_child(
    agent_dir: Path,
    child_code: str,
    timeout: float = 60.0,
    model_base_url: str | None = None,
    separate_home: bool = False,
):
    last = (None, "TIMEOUT")
    for _attempt in range(2):
        workdir = Path(tempfile.mkdtemp(prefix="state_smoke_"))
        child_cwd = workdir
        child_home = workdir
        if separate_home:
            child_cwd = workdir / "repo"
            child_home = workdir / "home"
            child_cwd.mkdir()
            child_home.mkdir()
        started = time.monotonic()
        env = dict(os.environ)
        env["HOME"] = str(child_home)
        if separate_home:
            env.pop("YUYAO_HOME", None)
        env["MSWEA_SILENT_STARTUP"] = "1"
        env["D2A_MODEL_BASE_URL"] = model_base_url or "http://127.0.0.1:1"
        env["D2A_MODEL_NAME"] = "litellm_proxy/mock"
        env["D2A_MODEL_API_KEY"] = "sk-mock-test"
        try:
            r = subprocess.run(
                [sys.executable, "-c", child_code],
                cwd=child_cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            out, err = (r.stdout or "", r.stderr or "")
        except subprocess.TimeoutExpired:
            last = (None, "TIMEOUT")
            time.sleep(0.1)
            shutil.rmtree(child_cwd / ".d2a_mcp_stdio", ignore_errors=True)
            continue
        res = None
        for ln in out.splitlines():
            if ln.startswith(RESULT_TAG):
                try:
                    res = json.loads(ln[len(RESULT_TAG) :])
                except Exception:
                    res = None
        stub_logs = []
        log_dirs = sorted(Path(workdir).rglob(".d2a_mcp_stdio"))
        for log_path in sorted(Path(workdir).rglob(".d2a_mcp_stdio/stub-*.jsonl")):
            rows = []
            try:
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            except Exception as exc:
                rows.append({"event": "log-read-error", "type": type(exc).__name__})
            stub_logs.append(
                {"file": log_path.name, "relative_path": str(log_path.relative_to(workdir)), "events": rows}
            )
        if isinstance(res, dict):
            res["_mcp_stdio_fixture"] = {
                "attempt": _attempt + 1,
                "wall_time_ms": round((time.monotonic() - started) * 1000, 3),
                "stub_logs": stub_logs,
            }
        for log_dir in reversed(log_dirs):
            shutil.rmtree(log_dir, ignore_errors=True)
        return (res, err)
    return last


def _none(res, err):
    return (res is None, f"the child process did not self-report a result (crash/timeout) err={err[-160:]!r}")


def chk_custom_cmd_face_exists(token, spec):
    """v6§7:436 "custom commands (`commands/<name>.md`) ... DROP IN A FILE -> `/<name>` IS CALLABLE".

    The other custom-command cases go NA when the build has no commands/*.md route, so this slot carries the
    "surface missing" verdict and judges it ONLY ONCE HERE; the other cases are not penalised again.

    Three-valued: the whole sequence produced no model request -> NA (there is not even a model-call route,
    judged elsewhere); the command body entered the context sent to the model -> PASS; the body never reached
    wire -> FAIL (the surface v6§7:436 explicitly requires is missing).
    """

    def _f(res, err):
        if res is None:
            return (False, _none(res, err)[1])
        rets = res.get("returns") or []
        if rets and any((sig in (rets[0] or "").lower() for sig in _CMD_UNKNOWN_SIGS)):
            return (
                False,
                f"""`/plainrun` replied "unrecognised command" => a `commands/plainrun.md` was placed but cannot be invoked out={(rets[0] or '')[:90]!r} ({spec})""",
            )
        reqs = res.get("reqs") or []
        if not reqs:
            return (
                True,
                "inconclusive: the whole sequence produced no model request (this build has no model-call route) -> skipped",
            )
        seen = any(((r.get("probes") or {}).get(token) for r in reqs))
        return (
            seen,
            f"the custom command body entered the context sent to the model (token={token})={seen} reqs={len(reqs)} ({spec}; a `commands/<name>.md` was placed but cannot be invoked, or its body never enters the context = this surface is unimplemented)",
        )

    return _f


_MCP_STDIO_STUB_SRC = '''\
#!/usr/bin/env python3
"""Deterministic, protocol-complete MCP stdio fixture.

It accepts both newline-delimited JSON-RPC and Content-Length framing, replies
to the MCP initialization/tool-discovery handshake, and intentionally remains
alive until its client closes stdin.
"""
import json
import os
import sys
import time
from pathlib import Path

LOG_DIR = Path.cwd() / ".d2a_mcp_stdio"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / ("stub-%d.jsonl" % os.getpid())
ENABLE_PROBE_TOOL = "--probe-tool" in sys.argv[1:]
POLLUTE_STDOUT = "--stdout-pollution" in sys.argv[1:]
PEER_PROFILE = next(
    (
        arg.split("=", 1)[1]
        for arg in sys.argv[1:]
        if arg.startswith("--peer-profile=")
    ),
    "",
)
WORKSPACE_TOKEN = os.environ.get("D2A_MCP_WORKSPACE_TOKEN", "")
RESOURCE_GENERATION = os.environ.get("D2A_MCP_RESOURCE_GENERATION", "")
PROBE_TOOL_NAME = os.environ.get("D2A_MCP_PROBE_TOOL_NAME", "probe_write")
STDOUT_GARBAGE = b"D2A_MCP_STDOUT_GARBAGE_9271\\n"
_pollution_sent = False
_list_generation = "A"
_loop_pages = 0
_ping_sent = False
_ping_ack = False
_PING_ID = "server-ping-peer-7806"
_PEER_COMMON_RESULT = "MCP_PEER_COMMON_RESULT_8109"
_PEER_LIST_A_RESULT = "MCP_PEER_LIST_A_RESULT_7804"
PROBE_TOOL = {
    "name": PROBE_TOOL_NAME,
    "description": "write a deterministic marker file",
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "text": {"type": "string"},
        },
        "required": ["path"],
    },
}
_BASE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "text": {"type": "string"},
    },
    "required": ["path"],
    "additionalProperties": False,
}
_VALID_NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "text": {"type": "string"},
        "options": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["safe"]},
                "flags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    "required": ["path", "options"],
    "additionalProperties": False,
}
_INVALID_NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "text": {"type": "string"},
        "options": {
            "type": "object",
            "properties": [],
            "required": "mode",
        },
    },
    "required": ["path"],
}

def tool(name, schema=None, description="deterministic Peer MCP tool"):
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else _BASE_SCHEMA,
    }

_PEER_NAMES = {
    "schema": "schema_nested_peer_7609",
    "unique_a": "unique_alpha_peer_7610",
    "unique_b": "unique_beta_peer_7611",
    "duplicate": "duplicate_peer_7612",
    "page_a": "page_alpha_peer_7703",
    "page_b": "page_beta_peer_7704",
    "loop": "page_loop_peer_7705",
    "list_a": "list_generation_a_peer_7804",
    "list_b": "list_generation_b_peer_7805",
    "ping": "ping_followup_peer_7806",
    "iserror": "result_map_peer_8109",
}

def log(event, payload=None):
    row = {"event": event, "monotonic_ns": time.monotonic_ns(), "pid": os.getpid()}
    if payload is not None:
        row["payload"] = payload
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\\n")

def read_message(stream):
    line = stream.readline()
    if not line:
        return None, None
    if line.lower().startswith(b"content-length:"):
        try:
            size = int(line.split(b":", 1)[1].strip())
        except Exception:
            log("bad-content-length", line.decode("utf-8", "replace"))
            return {}, "content-length"
        while True:
            header = stream.readline()
            if not header or header in (b"\\n", b"\\r\\n"):
                break
        body = stream.read(size)
        return json.loads(body.decode("utf-8")), "content-length"
    return json.loads(line.decode("utf-8")), "newline"

POLLUTION_SETTLE_S = 1.0

def send_message(stream, payload, framing):
    global _pollution_sent
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    polluting = POLLUTE_STDOUT and not _pollution_sent
    if polluting:
        _pollution_sent = True
        log("stdout-pollution", {
            "bytes": len(STDOUT_GARBAGE),
            "hex": STDOUT_GARBAGE.hex(),
        })
    # Evidence before any observable effect: the client may kill this process the moment it has the response, so the send log must be written first.
    log("send", payload)
    if polluting:
        stream.write(STDOUT_GARBAGE)
        stream.flush()
        # Separate the garbage line from the legal response by 1s: any reader necessarily reads the garbage line on its own first, ruling out same-buffer swallowing.
        time.sleep(POLLUTION_SETTLE_S)
    if framing == "content-length":
        stream.write(("Content-Length: %d\\r\\n\\r\\n" % len(body)).encode("ascii") + body)
    else:
        stream.write(body + b"\\n")
    stream.flush()

def send_error(stream, request_id, code, message, framing):
    send_message(stream, {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }, framing)

def peer_list_result(params):
    global _loop_pages
    cursor = (params or {}).get("cursor")
    if PEER_PROFILE == "schema-control":
        return {"tools": [tool(_PEER_NAMES["schema"], _VALID_NESTED_SCHEMA)]}
    if PEER_PROFILE == "schema-invalid":
        return {"tools": [tool(_PEER_NAMES["schema"], _INVALID_NESTED_SCHEMA)]}
    if PEER_PROFILE == "names-control":
        beta_schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "text": {"type": "string"},
                "count": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "count"],
            "additionalProperties": False,
        }
        return {"tools": [
            tool(_PEER_NAMES["unique_a"]),
            tool(_PEER_NAMES["unique_b"], beta_schema),
        ]}
    if PEER_PROFILE == "names-duplicate":
        alternate = {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        }
        return {"tools": [
            tool(_PEER_NAMES["duplicate"], _BASE_SCHEMA, "duplicate first schema"),
            tool(_PEER_NAMES["duplicate"], alternate, "duplicate second schema"),
        ]}
    if PEER_PROFILE == "pagination-control":
        return {"tools": [
            tool(_PEER_NAMES["page_a"]),
            tool(_PEER_NAMES["page_b"]),
        ]}
    if PEER_PROFILE == "pagination-finite":
        if cursor is None:
            return {
                "tools": [tool(_PEER_NAMES["page_a"])],
                "nextCursor": "opaque-page-two-peer-7703",
            }
        if cursor == "opaque-page-two-peer-7703":
            return {"tools": [tool(_PEER_NAMES["page_b"])]}
        raise ValueError("unexpected finite cursor: " + repr(cursor))
    if PEER_PROFILE == "pagination-loop":
        if cursor is None:
            _loop_pages = 1
        elif cursor == "opaque-loop-peer-7705":
            _loop_pages += 1
        else:
            raise ValueError("unexpected loop cursor: " + repr(cursor))
        if _loop_pages > 2:
            raise ValueError("cursor cycle was not stopped by client")
        return {
            "tools": [tool(_PEER_NAMES["loop"])],
            "nextCursor": "opaque-loop-peer-7705",
        }
    if PEER_PROFILE in ("listchanged-control", "listchanged-target"):
        name = _PEER_NAMES["list_a"] if _list_generation == "A" else _PEER_NAMES["list_b"]
        return {"tools": [tool(name)]}
    if PEER_PROFILE in ("ping-control", "ping-target"):
        return {"tools": [tool(_PEER_NAMES["ping"])]}
    if PEER_PROFILE in ("iserror-false", "iserror-true"):
        return {"tools": [tool(_PEER_NAMES["iserror"])]}
    return {"tools": [PROBE_TOOL] if ENABLE_PROBE_TOOL else []}

def peer_allowed_names():
    if PEER_PROFILE.startswith("schema-"):
        return {_PEER_NAMES["schema"]}
    if PEER_PROFILE == "names-control":
        return {_PEER_NAMES["unique_a"], _PEER_NAMES["unique_b"]}
    if PEER_PROFILE == "names-duplicate":
        return {_PEER_NAMES["duplicate"]}
    if PEER_PROFILE in ("pagination-control", "pagination-finite"):
        return {_PEER_NAMES["page_a"], _PEER_NAMES["page_b"]}
    if PEER_PROFILE == "pagination-loop":
        return {_PEER_NAMES["loop"]}
    if PEER_PROFILE in ("listchanged-control", "listchanged-target"):
        return {
            _PEER_NAMES["list_a"] if _list_generation == "A"
            else _PEER_NAMES["list_b"]
        }
    if PEER_PROFILE in ("ping-control", "ping-target"):
        return {_PEER_NAMES["ping"]}
    if PEER_PROFILE in ("iserror-false", "iserror-true"):
        return {_PEER_NAMES["iserror"]}
    return {PROBE_TOOL["name"]} if ENABLE_PROBE_TOOL else set()

def apply_effect(args, fallback):
    marker = Path(str(args.get("path") or fallback))
    text = str(args.get("text") or "MCP_PEER_EFFECT\\n")
    marker.write_text(text, encoding="utf-8")
    log("tool-effect", {
        "profile": PEER_PROFILE,
        "path": str(marker),
        "text": text,
    })
    return marker

log("start", {
    "cwd": str(Path.cwd().resolve()),
    "workspace_token": WORKSPACE_TOKEN,
    "resource_generation": RESOURCE_GENERATION,
    "probe_tool_name": PROBE_TOOL_NAME,
    "probe_tool": ENABLE_PROBE_TOOL,
    "stdout_pollution": POLLUTE_STDOUT,
    "peer_profile": PEER_PROFILE,
})
while True:
    try:
        request, framing = read_message(sys.stdin.buffer)
    except Exception as exc:
        log("decode-error", {"type": type(exc).__name__, "message": str(exc)})
        continue
    if request is None:
        log("stdin-eof")
        break
    log("recv", request)
    if not isinstance(request, dict):
        continue
    request_id = request.get("id")
    method = request.get("method") or ""
    if PEER_PROFILE == "ping-target" and request_id == _PING_ID and not method:
        exact = (
            request.get("jsonrpc") == "2.0"
            and request.get("result") == {}
            and "error" not in request
            and set(request) == {"jsonrpc", "id", "result"}
        )
        _ping_ack = exact
        log("peer-ping-response", {"exact": exact, "payload": request})
    elif method == "initialize" and request_id is not None:
        requested = (request.get("params") or {}).get("protocolVersion")
        negotiated = (
            "2024-11-05"
            if PEER_PROFILE in ("ping-control", "ping-target")
            else requested or "2024-11-05"
        )
        list_changed = PEER_PROFILE in (
            "listchanged-control",
            "listchanged-target",
        )
        send_message(sys.stdout.buffer, {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": list_changed}},
                "serverInfo": {"name": "d2a-state-fixture", "version": "1.0.0"}
            }
        }, framing)
    elif method in ("notifications/initialized", "initialized"):
        log("initialized")
    elif method in ("tools/list", "list_tools") and request_id is not None:
        params = request.get("params") or {}
        try:
            result = peer_list_result(params)
        except ValueError as exc:
            log("peer-list-error", {
                "profile": PEER_PROFILE,
                "cursor": params.get("cursor"),
                "message": str(exc),
            })
            send_error(sys.stdout.buffer, request_id, -32602, str(exc), framing)
            continue
        send_message(sys.stdout.buffer, {
            "jsonrpc": "2.0", "id": request_id,
            "result": result,
        }, framing)
        log("peer-tools-page", {
            "profile": PEER_PROFILE,
            "cursor": params.get("cursor"),
            "nextCursor": result.get("nextCursor"),
            "names": [
                item.get("name")
                for item in result.get("tools") or []
                if isinstance(item, dict)
            ],
        })
        if PEER_PROFILE == "ping-target" and not _ping_sent:
            _ping_sent = True
            ping_request = {
                "jsonrpc": "2.0",
                "id": _PING_ID,
                "method": "ping",
            }
            send_message(sys.stdout.buffer, ping_request, framing)
            log("peer-ping-request", ping_request)
    elif method in ("tools/call", "call_tool") and request_id is not None:
        params = request.get("params") or {}
        tool_name = params.get("name") or params.get("tool") or ""
        args = params.get("arguments") or params.get("args") or {}
        if PEER_PROFILE == "ping-target" and _ping_sent and not _ping_ack:
            log("peer-ping-barrier", {
                "tool": tool_name,
                "request_id": request_id,
                "ping_ack": _ping_ack,
            })
            send_error(
                sys.stdout.buffer,
                request_id,
                -32001,
                "server ping was not acknowledged",
                framing,
            )
        elif PEER_PROFILE == "iserror-true" and tool_name == _PEER_NAMES["iserror"]:
            send_message(sys.stdout.buffer, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": _PEER_COMMON_RESULT}],
                    "isError": True,
                },
            }, framing)
            log("peer-iserror-result", {
                "isError": True,
                "effect": False,
                "tool": tool_name,
            })
        elif PEER_PROFILE and tool_name in peer_allowed_names():
            marker = apply_effect(args, "mcp_peer_out.txt")
            result_text = (
                _PEER_COMMON_RESULT
                if PEER_PROFILE == "iserror-false"
                else _PEER_LIST_A_RESULT
                if PEER_PROFILE == "listchanged-target"
                    and tool_name == _PEER_NAMES["list_a"]
                else "wrote " + str(marker)
            )
            send_message(sys.stdout.buffer, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            }, framing)
            if PEER_PROFILE == "iserror-false":
                log("peer-iserror-result", {
                    "isError": False,
                    "effect": True,
                    "tool": tool_name,
                })
            if PEER_PROFILE == "listchanged-target" \\
                    and tool_name == _PEER_NAMES["list_a"]:
                _list_generation = "B"
                notification = {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                }
                send_message(sys.stdout.buffer, notification, framing)
                log("peer-list-changed", {
                    "from": "A",
                    "to": "B",
                    "payload": notification,
                })
        elif ENABLE_PROBE_TOOL and tool_name == PROBE_TOOL["name"]:
            marker = Path(str(args.get("path") or "mcp_probe_out.txt"))
            text = str(args.get("text") or "MCP_PROBE_WROTE_9271\\n")
            marker.write_text(text, encoding="utf-8")
            send_message(sys.stdout.buffer, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "wrote " + str(marker)}],
                    "isError": False,
                },
            }, framing)
        else:
            send_error(
                sys.stdout.buffer,
                request_id,
                -32602,
                "unknown probe tool: " + str(tool_name),
                framing,
            )
    elif method == "ping" and request_id is not None:
        send_message(sys.stdout.buffer, {
            "jsonrpc": "2.0", "id": request_id, "result": {}
        }, framing)
    elif request_id is not None:
        send_error(
            sys.stdout.buffer,
            request_id,
            -32601,
            "method not found: " + method,
            framing,
        )
log("stop")
'''
_MCP_STDIO_STUB = Path(tempfile.gettempdir()) / "d2a_state_mcp_stdio_fixture.py"


def _write_mcp_stdio_stub() -> Path:
    """Materialize the embedded fixture for the state harness container."""
    _MCP_STDIO_STUB.write_text(_MCP_STDIO_STUB_SRC, encoding="utf-8")
    _MCP_STDIO_STUB.chmod(448)
    return _MCP_STDIO_STUB


def _cleanup_mcp_stdio_stub() -> None:
    """Remove the shared executable; per-case logs are removed by `_run_child`."""
    try:
        _MCP_STDIO_STUB.unlink()
    except FileNotFoundError:
        pass


_write_mcp_stdio_stub()
atexit.register(_cleanup_mcp_stdio_stub)


_SESSION_MEMORY_QUERY = "zirconium quasar heliotrope"
_SESSION_MEMORY_WARM_QUERY = "warmup_only_session_6401"
_SESSION_MEMORY_MARKERS = ("MEMORY_SESSION_ALPHA_6402", "MEMORY_SESSION_BETA_6403", "MEMORY_SESSION_GAMMA_6404")
_SESSION_MEMORY_DECOY = "DECOY_SESSION_"
_SESSION_MEMORY_TARGET_PROMPT = "SESSION_MEMORY_TARGET_ROUTE_6406"
_SESSION_MEMORY_SMALL = 2000
_SESSION_MEMORY_LARGE = 120000
_SESSION_MEMORY_RSS_MARGIN = 4 * 1024 * 1024
_SESSION_MEMORY_RSS_PROBE = 16 * 1024 * 1024
_SESSION_CLOSE_PROMPT = "SESSION_CLOSE_ROUTE_6501"
_SESSION_CLOSE_COMMAND = "__SESSION_CLOSE_COMMAND__"
_SESSION_CLOSE_PID = "session_close_helper.pid"
_SESSION_CLOSE_STATE = "session_close_helper.state"
_SESSION_CLOSE_HEARTBEAT = "session_close_helper.heartbeat"
_SESSION_CLOSE_LEASE = "session_close_helper.lease"
_SESSION_COPY_PROMPT = "SESSION_COPY_ROUTE_6601"
_SESSION_COPY_ASCII = "COPY_SESSION_ASCII_6602 exact bytes"
_SESSION_COPY_UTF8 = "COPY_SESSION_UTF8_6603 雪豹 café Δ"
_SESSION_COPY_CAPTURE = "session_clipboard_capture.bin"
_SESSION_COPY_META = "session_clipboard_meta.json"


def _session_child_code(agent_src: str, scenario: dict) -> str:
    """Build one fresh public-AgentSession worker for a Session variant."""
    template = '''
from __future__ import annotations
import gc
import importlib
import json
import locale
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path[:0] = [__AGENT_SRC__, os.path.dirname(__AGENT_SRC__)]
scenario = json.loads(__SCENARIO__)
result_tag = __RESULT_TAG__
session = None


def emit(value):
    sys.stdout.write("\\n" + result_tag + json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ) + "\\n")
    sys.stdout.flush()


def public_method(obj, name):
    method = getattr(obj, name, None)
    if not callable(method):
        raise RuntimeError("PUBLIC_METHOD_MISSING_" + name.upper())
    return method


def call_record(obj, name, *args):
    try:
        value = public_method(obj, name)(*args)
        return {
            "ok": True,
            "return_type": type(value).__name__,
            "return_is_str": isinstance(value, str),
            "return": value[:500] if isinstance(value, str) else repr(value)[:500],
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


def rss_bytes():
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
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
        "baseline": baseline,
        "peak": peak,
        "after": after,
        "delta": delta,
        "samples": len(samples),
    }


def rss_sampler_probe(size):
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


def alive_with(real_kill, pid):
    if not pid:
        return False
    try:
        real_kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def fd_links(pid):
    out = {}
    try:
        entries = list((Path("/proc") / str(pid) / "fd").iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            out[entry.name] = os.readlink(entry)
        except OSError:
            pass
    return out


def read_int(path):
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def wait_for(path, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


try:
    try:
        api = importlib.import_module("yuyao.agent_api")
    except ModuleNotFoundError as exc:
        if exc.name in {"yuyao", "yuyao.agent_api"}:
            emit({
                "session": True,
                "operation": scenario.get("operation"),
                "state": "unsupported",
                "reason": "PUBLIC_AGENT_API_MISSING",
            })
            raise SystemExit(0)
        raise
    factory = getattr(api, "create_session", None)
    if not callable(factory):
        emit({
            "session": True,
            "operation": scenario.get("operation"),
            "state": "unsupported",
            "reason": "PUBLIC_FACTORY_MISSING",
        })
        raise SystemExit(0)

    root = Path.cwd()
    repo = root / "repo"
    repo.mkdir()
    operation = scenario["operation"]

    if operation == "copy":
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        capture = root / scenario["capture"]
        meta = root / scenario["meta"]
        helper = fake_bin / "pbcopy"
        helper.write_text(
            "#!" + sys.executable + "\\n"
            "import json, os, sys\\n"
            "data = sys.stdin.buffer.read()\\n"
            "open(os.environ['D2A_SESSION_CLIPBOARD_CAPTURE'], 'wb').write(data)\\n"
            "open(os.environ['D2A_SESSION_CLIPBOARD_META'], 'w', encoding='ascii').write("
            "json.dumps({'argv': sys.argv, 'bytes': len(data)}))\\n",
            encoding="utf-8",
        )
        helper.chmod(0o700)
        os.environ["PATH"] = str(fake_bin) + os.pathsep + os.environ.get("PATH", "")
        os.environ["D2A_SESSION_CLIPBOARD_CAPTURE"] = str(capture)
        os.environ["D2A_SESSION_CLIPBOARD_META"] = str(meta)
        try:
            locale.setlocale(locale.LC_ALL, "C")
        except locale.Error:
            pass
        session = factory(mode="yolo", role="execute", cwd=str(repo))
        chat = call_record(session, "chat", scenario["prompt"])
        copied = call_record(session, "dispatch", "/copy")
        closed = call_record(session, "close")
        session = None
        raw = capture.read_bytes() if capture.exists() else None
        try:
            helper_meta = json.loads(meta.read_text(encoding="ascii"))
        except (OSError, ValueError):
            helper_meta = None
        emit({
            "session": True,
            "operation": operation,
            "state": "ok",
            "locale": {
                "preferred": locale.getpreferredencoding(False),
                "filesystem": sys.getfilesystemencoding(),
                "utf8_mode": int(sys.flags.utf8_mode),
                "lc_all": os.environ.get("LC_ALL", ""),
                "lang": os.environ.get("LANG", ""),
            },
            "chat": chat,
            "dispatch": copied,
            "close": closed,
            "capture_present": raw is not None,
            "capture_hex": raw.hex() if raw is not None else None,
            "capture_length": len(raw) if raw is not None else None,
            "helper_meta": helper_meta,
        })

    elif operation == "memory":
        (repo / "memory").mkdir()
        top = (
            "# Memory\\n"
            "- " + scenario["markers"][0] + " zirconium quasar heliotrope "
            "zirconium quasar heliotrope\\n"
            "- " + scenario["markers"][1] + " zirconium quasar\\n"
            "- " + scenario["markers"][2] + " zirconium\\n"
            "- " + scenario["warm_query"] + " warm-up control\\n"
        )
        (repo / "MEMORY.md").write_text(top, encoding="utf-8")
        with (repo / "memory" / "archive-2000-01-01.md").open(
                "w", encoding="utf-8") as handle:
            handle.write("# Archived memory\\n")
            for index in range(int(scenario["entries"])):
                handle.write(
                    "- " + scenario["decoy"] + ("%06d" % index)
                    + " ordinary deployment preference number " + str(index) + "\\n"
                )
        session = factory(mode="yolo", role="execute", cwd=str(repo))
        index_call = call_record(session, "dispatch", "/memory status")
        gc.collect()
        time.sleep(0.05)
        measured, rss = sampled_call(
            lambda: call_record(session, "chat", scenario["target_prompt"])
        )
        sampler = rss_sampler_probe(int(scenario["rss_probe_bytes"]))
        closed = call_record(session, "close")
        session = None
        emit({
            "session": True,
            "operation": operation,
            "state": "ok",
            "entries": int(scenario["entries"]),
            "index_call": index_call,
            "measured": measured,
            "rss": rss,
            "rss_sampler_probe": sampler,
            "close": closed,
        })

    elif operation == "close":
        pid_path = root / scenario["pid_file"]
        state_path = root / scenario["state_file"]
        heartbeat_path = root / scenario["heartbeat_file"]
        lease_path = root / scenario["lease_file"]
        helper = repo / "close_helper.py"
        helper.write_text(
            "from pathlib import Path\\n"
            "import os, signal, sys, time\\n"
            "pid = Path(os.environ['D2A_SESSION_CLOSE_PID'])\\n"
            "state = Path(os.environ['D2A_SESSION_CLOSE_STATE'])\\n"
            "heartbeat = Path(os.environ['D2A_SESSION_CLOSE_HEARTBEAT'])\\n"
            "lease = open(os.environ['D2A_SESSION_CLOSE_LEASE'], 'w', encoding='ascii')\\n"
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
            "print('SESSION_CLOSE_HELPER_READY', flush=True)\\n"
            "while True:\\n"
            "    counter += 1\\n"
            "    heartbeat.write_text(str(counter), encoding='ascii')\\n"
            "    time.sleep(0.02)\\n",
            encoding="utf-8",
        )
        os.environ["D2A_SESSION_CLOSE_PID"] = str(pid_path)
        os.environ["D2A_SESSION_CLOSE_STATE"] = str(state_path)
        os.environ["D2A_SESSION_CLOSE_HEARTBEAT"] = str(heartbeat_path)
        os.environ["D2A_SESSION_CLOSE_LEASE"] = str(lease_path)
        session = factory(mode="yolo", role="execute", cwd=str(repo))
        mode_call = call_record(session, "dispatch", "/mode yolo")
        route = call_record(session, "chat", scenario["prompt"])
        if not wait_for(pid_path):
            emit({
                "session": True,
                "operation": operation,
                "state": "unsupported",
                "reason": "PUBLIC_BACKGROUND_RESOURCE_NOT_STARTED",
                "mode_call": mode_call,
                "route": route,
            })
            raise SystemExit(0)
        pid = read_int(pid_path)
        real_kill = os.kill
        real_killpg = os.killpg
        real_getpgid = os.getpgid
        real_terminate = subprocess.Popen.terminate
        real_proc_kill = subprocess.Popen.kill
        real_wait = subprocess.Popen.wait
        try:
            target_pgid = real_getpgid(int(pid))
        except (OSError, TypeError, ValueError):
            target_pgid = None
        phase = {"name": "setup"}
        signal_ops = []

        def belongs(candidate_pid):
            try:
                return target_pgid is not None \\
                    and real_getpgid(int(candidate_pid)) == target_pgid
            except (OSError, TypeError, ValueError):
                return int(candidate_pid or -1) == int(pid or -2)

        def faulted():
            return bool(scenario.get("inject_fault")) and phase["name"] == "first"

        def note(op, target, sig=None, will_fault=False):
            signal_ops.append({
                "phase": phase["name"],
                "op": op,
                "target": int(target),
                "signal": int(sig) if sig is not None else None,
                "faulted": bool(will_fault),
            })

        def patched_killpg(pgid, sig):
            hit = target_pgid is not None and int(pgid) == int(target_pgid)
            reject = hit and faulted() and int(sig) != 0
            if hit:
                note("os.killpg", pgid, sig, reject)
            if reject:
                raise OSError("SESSION_TRANSIENT_KILLPG")
            return real_killpg(pgid, sig)

        def patched_kill(candidate_pid, sig):
            hit = belongs(candidate_pid)
            reject = hit and faulted() and int(sig) != 0
            if hit and int(sig) != 0:
                note("os.kill", candidate_pid, sig, reject)
            if reject:
                raise OSError("SESSION_TRANSIENT_KILL")
            return real_kill(candidate_pid, sig)

        def patched_terminate(proc):
            hit = belongs(proc.pid)
            reject = hit and faulted()
            if hit:
                note("Popen.terminate", proc.pid, signal.SIGTERM, reject)
            if reject:
                raise OSError("SESSION_TRANSIENT_TERMINATE")
            return real_terminate(proc)

        def patched_proc_kill(proc):
            hit = belongs(proc.pid)
            reject = hit and faulted()
            if hit:
                note("Popen.kill", proc.pid, signal.SIGKILL, reject)
            if reject:
                raise OSError("SESSION_TRANSIENT_PROC_KILL")
            return real_proc_kill(proc)

        def patched_wait(proc, timeout=None):
            hit = belongs(proc.pid)
            reject = hit and faulted()
            if hit:
                note("Popen.wait", proc.pid, None, reject)
            if reject:
                raise subprocess.TimeoutExpired("SESSION_TRANSIENT_WAIT", timeout or 0)
            return real_wait(proc, timeout=timeout)

        os.killpg = patched_killpg
        os.kill = patched_kill
        subprocess.Popen.terminate = patched_terminate
        subprocess.Popen.kill = patched_proc_kill
        subprocess.Popen.wait = patched_wait

        child_links = {
            link for fd, link in fd_links(pid).items()
            if int(fd) <= 2 and (
                link.startswith("pipe:[") or link.startswith("socket:[")
            )
        }

        def snapshot(label):
            parent = fd_links(os.getpid())
            child = fd_links(pid)
            related = {
                fd: link for fd, link in parent.items() if link in child_links
            }
            lease_fds = {
                fd: link for fd, link in child.items()
                if link == str(lease_path)
            }
            try:
                state_text = state_path.read_text(encoding="ascii")
            except OSError:
                state_text = ""
            return {
                "label": label,
                "pid_alive": alive_with(real_kill, pid),
                "related_fd_count": len(related),
                "related_fds": related,
                "lease_fd_count": len(lease_fds),
                "lease_fds": lease_fds,
                "heartbeat": read_int(heartbeat_path),
                "helper_state": state_text,
            }

        time.sleep(0.08)
        before = snapshot("before")
        phase["name"] = "first"
        first_close = call_record(session, "close")
        phase["name"] = "observe-first"
        after_first_a = snapshot("after-first-a")
        time.sleep(0.16)
        after_first_b = snapshot("after-first-b")

        phase["name"] = "second"
        second_close = call_record(session, "close")
        phase["name"] = "observe-second"
        time.sleep(0.12)
        after_second_a = snapshot("after-second-a")
        time.sleep(0.16)
        after_second_b = snapshot("after-second-b")

        phase["name"] = "third"
        ops_before_third = len(signal_ops)
        third_close = call_record(session, "close")
        phase["name"] = "observe-third"
        time.sleep(0.08)
        after_third = snapshot("after-third")
        third_signal_ops = len(signal_ops) - ops_before_third
        session = None

        os.killpg = real_killpg
        os.kill = real_kill
        subprocess.Popen.terminate = real_terminate
        subprocess.Popen.kill = real_proc_kill
        subprocess.Popen.wait = real_wait
        harness_cleanup = False
        if alive_with(real_kill, pid):
            harness_cleanup = True
            try:
                if target_pgid is not None:
                    real_killpg(target_pgid, signal.SIGKILL)
                else:
                    real_kill(pid, signal.SIGKILL)
            except OSError:
                pass

        emit({
            "session": True,
            "operation": operation,
            "state": "ok",
            "fault_injected": bool(scenario.get("inject_fault")),
            "pid": pid,
            "pgid": target_pgid,
            "resource_links": sorted(child_links),
            "mode_call": mode_call,
            "route": route,
            "first_close": first_close,
            "second_close": second_close,
            "third_close": third_close,
            "signal_ops": signal_ops,
            "third_signal_ops": third_signal_ops,
            "before": before,
            "after_first_a": after_first_a,
            "after_first_b": after_first_b,
            "after_second_a": after_second_a,
            "after_second_b": after_second_b,
            "after_third": after_third,
            "harness_cleanup": harness_cleanup,
        })
    else:
        raise RuntimeError("UNKNOWN_SESSION_OPERATION")
except SystemExit:
    raise
except BaseException as exc:
    emit({
        "session": True,
        "operation": scenario.get("operation"),
        "state": "error",
        "error_type": type(exc).__name__,
        "error": str(exc)[:300],
    })
finally:
    if session is not None:
        try:
            public_method(session, "close")()
        except BaseException:
            pass
'''
    return (
        template.replace("__AGENT_SRC__", repr(agent_src))
        .replace(
            "__SCENARIO__",
            repr(json.dumps(scenario, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        .replace("__RESULT_TAG__", repr(RESULT_TAG))
    )


def _run_session_api_variants(agent_dir: Path, case: dict, cfg: dict, model_base_url: str):
    """Run each Session public-API scenario in a fresh process/workspace."""
    rows = []
    first_request = len(cfg["requests"])
    for variant in case["session_api_variants"]:
        before = len(cfg["requests"])
        workdir = Path(tempfile.mkdtemp(prefix="state_session_api_"))
        home, temp = (workdir / "home", workdir / "tmp")
        home.mkdir()
        temp.mkdir()
        merged_stub = {**_DEFAULT_STUB, **(case.get("stub") or {}), **(variant.get("stub") or {})}
        close_command = (
            "exec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(str(workdir / "repo" / "close_helper.py"))
        )
        for key, value in merged_stub.items():
            if key == "requests":
                continue
            try:
                encoded = json.dumps(value)
                encoded = encoded.replace(_SESSION_CLOSE_COMMAND, close_command)
                value = json.loads(encoded)
            except (TypeError, ValueError):
                pass
            cfg[key] = value
        cfg["rounds_base"] = before
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(temp),
                "TMP": str(temp),
                "TEMP": str(temp),
                "MSWEA_SILENT_STARTUP": "1",
                "D2A_MODEL_BASE_URL": model_base_url,
                "D2A_MODEL_NAME": "litellm_proxy/mock",
                "D2A_MODEL_API_KEY": "sk-mock-test",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if variant.get("operation") == "copy":
            env.update({"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"})
        scenario = {
            key: value for key, value in variant.items() if key not in {"label", "stub", "child_timeout"}
        }
        code = _session_child_code(str((agent_dir / "src").resolve()), scenario)
        process = _fork_process(
            (sys.executable, "-c", code),
            cwd=workdir,
            env=env,
            timeout=float(variant.get("child_timeout") or case.get("child_timeout") or 90.0),
        )
        result = _fork_parse_child(process["stdout"])
        requests = list(cfg["requests"])[before:]
        if isinstance(result, dict):
            result["_session_process"] = {
                key: process[key] for key in ("returncode", "timed_out", "cleanup_complete", "start_error")
            }
            result["reqs"] = requests
        rows.append(
            {
                "label": variant["label"],
                "operation": variant["operation"],
                "result": result,
                "requests": requests,
                "stderr_tail": process["stderr"][-800:],
            }
        )
        shutil.rmtree(workdir, ignore_errors=True)
    return ({"session_variants": rows, "reqs": list(cfg["requests"])[first_request:]}, "")


def _session_rows(res):
    if not isinstance(res, dict):
        return None
    raw = res.get("session_variants")
    if not isinstance(raw, list):
        return None
    rows = {}
    for row in raw:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            return None
        if row["label"] in rows:
            return None
        rows[row["label"]] = row
    return rows


def _session_result(row):
    result = row.get("result") if isinstance(row, dict) else None
    return result if isinstance(result, dict) else None


def _session_process_healthy(result):
    process = (result or {}).get("_session_process") or {}
    return (
        process.get("returncode") == 0
        and process.get("timed_out") is False
        and (process.get("cleanup_complete") is True)
        and (not process.get("start_error"))
    )


def _session_unsupported(result):
    return isinstance(result, dict) and result.get("state") == "unsupported"


def _session_tool_blob(row, markers):
    markers = tuple(markers)
    candidates = []
    for request in (row or {}).get("requests") or []:
        texts = request.get("tool_result_texts") or []
        blob = "\n".join((str(text) for text in texts))
        if any((marker in blob for marker in markers)):
            candidates.append(blob)
    return candidates[-1] if candidates else ""


def _session_topk_facts(row):
    blob = _session_tool_blob(row, _SESSION_MEMORY_MARKERS)
    positions = [blob.find(marker) for marker in _SESSION_MEMORY_MARKERS]
    return {
        "tool_result": bool(blob),
        "all_markers": all((position >= 0 for position in positions)),
        "ordered": (
            all((left < right for left, right in zip(positions, positions[1:])))
            if all((position >= 0 for position in positions))
            else False
        ),
        "one_each": all((blob.count(marker) == 1 for marker in _SESSION_MEMORY_MARKERS)),
        "no_decoy": _SESSION_MEMORY_DECOY not in blob,
        "preview": blob[:500],
    }


def chk_memory_query_topk_residency_control(res, err):
    rows = _session_rows(res)
    result = _session_result((rows or {}).get("control"))
    if rows is None or set(rows) != {"control"} or result is None:
        return (False, f"memory top-k/RSS control incomplete rows={rows!r} err={err[-160:]!r}")
    if _session_unsupported(result):
        return (
            True,
            f"inconclusive: the public AgentSession chat/memory_search path is unavailable reason={result.get('reason')}",
        )
    if result.get("state") != "ok" or not _session_process_healthy(result):
        return (False, f"memory control worker unhealthy result={result!r}")
    topk = _session_topk_facts(rows["control"])
    rss = result.get("rss") or {}
    probe = result.get("rss_sampler_probe") or {}
    rss_available = all((isinstance(rss.get(key), int) for key in ("baseline", "peak", "after", "delta")))
    probe_live = (
        isinstance(probe.get("delta"), int)
        and probe["delta"] >= _SESSION_MEMORY_RSS_PROBE // 2
        and (int(probe.get("samples") or 0) > 0)
    )
    if not rss_available or not probe_live:
        return (
            True,
            f"inconclusive: no trustworthy RSS/retained observation rss={rss} sampler_probe={probe}",
        )
    facts = {
        "worker": True,
        "public_calls": (result.get("index_call") or {}).get("return_is_str") is True
        and (result.get("measured") or {}).get("return_is_str") is True,
        "rss_available": rss_available,
        "sampler_live": probe_live,
        **{f"topk_{key}": value for key, value in topk.items() if key != "preview"},
    }
    ok = all(facts.values())
    return (
        ok,
        f"memory_query_topk_residency zero-weight control={ok} facts={facts} rss={rss} probe_delta={probe.get('delta')} preview={topk['preview']!r}",
    )


def chk_memory_query_topk_residency(res, err):
    rows = _session_rows(res)
    if rows is None or set(rows) != {"small", "large"}:
        return (False, f"memory residency target matrix incomplete rows={rows!r}")
    results = {label: _session_result(rows[label]) for label in ("small", "large")}
    if any((_session_unsupported(result) for result in results.values())):
        return (True, "inconclusive: the public memory_search/RSS matrix is unavailable")
    if not all(
        (
            result and result.get("state") == "ok" and _session_process_healthy(result)
            for result in results.values()
        )
    ):
        return (False, f"memory residency workers unhealthy results={results!r}")
    topk = {label: _session_topk_facts(rows[label]) for label in ("small", "large")}
    rss = {label: results[label].get("rss") or {} for label in results}
    probes = {label: results[label].get("rss_sampler_probe") or {} for label in results}
    observed = all(
        (
            all((isinstance(rss[label].get(key), int) for key in ("baseline", "peak", "after", "delta")))
            and int(rss[label].get("samples") or 0) > 0
            and (probes[label].get("allocated") == _SESSION_MEMORY_RSS_PROBE)
            for label in ("small", "large")
        )
    )
    if not observed:
        return (
            True,
            f"inconclusive: no RSS/retained observation rss={rss} probes={probes}",
        )
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
    same_topk = [topk["small"][key] for key in ("all_markers", "ordered", "one_each", "no_decoy")] == [
        topk["large"][key] for key in ("all_markers", "ordered", "one_each", "no_decoy")
    ]
    small_delta = int(rss["small"]["delta"])
    large_delta = int(rss["large"]["delta"])
    bounded = large_delta <= small_delta + _SESSION_MEMORY_RSS_MARGIN
    ok = functional and same_topk and bounded
    return (
        ok,
        f"memory query transient residency O(k)={ok} fixed-k result/order={functional and same_topk} small({_SESSION_MEMORY_SMALL}) delta={small_delta} large({_SESSION_MEMORY_LARGE}) delta={large_delta} allowed_margin={_SESSION_MEMORY_RSS_MARGIN} bounded={bounded} topk={{'small': {topk['small']}, 'large': {topk['large']}}}",
    )


def _session_close_base(result):
    before = (result or {}).get("before") or {}
    route = (result or {}).get("route") or {}
    return {
        "public_chat": route.get("return_is_str") is True,
        "pid_started": before.get("pid_alive") is True,
        "fd_observed": int(before.get("lease_fd_count") or 0) == 1,
        "state_started": "START" in str(before.get("helper_state") or ""),
        "heartbeat_started": isinstance(before.get("heartbeat"), int),
    }


def chk_agent_api_close_retry_incomplete_control(res, err):
    rows = _session_rows(res)
    result = _session_result((rows or {}).get("control"))
    if rows is None or set(rows) != {"control"} or result is None:
        return (False, f"close healthy control incomplete rows={rows!r}")
    if _session_unsupported(result):
        return (
            True,
            f"inconclusive: public AgentSession background resources / close unavailable reason={result.get('reason')}",
        )
    if result.get("state") != "ok" or not _session_process_healthy(result):
        return (False, f"close control worker unhealthy result={result!r}")
    base = _session_close_base(result)
    after = result.get("after_first_b") or {}
    after_second = result.get("after_second_b") or {}
    first_ops = [row for row in result.get("signal_ops") or [] if row.get("phase") == "first"]
    facts = {
        **base,
        "cleanup_attempted": bool(first_ops),
        "pid_closed": after.get("pid_alive") is False,
        "fd_closed": int(after.get("lease_fd_count") or 0) == 0,
        "heartbeat_stopped": after.get("heartbeat") == after_second.get("heartbeat"),
        "repeat_absorbed": not [
            row for row in result.get("signal_ops") or [] if row.get("phase") in {"second", "third"}
        ],
        "close_returns": all(
            (
                (result.get(name) or {}).get("ok") is True
                for name in ("first_close", "second_close", "third_close")
            )
        ),
    }
    ok = all(facts.values())
    return (
        ok,
        f"Agent API healthy close PID/FD/state control={ok} facts={facts} ops={result.get('signal_ops')} after={after}",
    )


def chk_agent_api_close_retry_incomplete(res, err):
    rows = _session_rows(res)
    result = _session_result((rows or {}).get("target"))
    if rows is None or set(rows) != {"target"} or result is None:
        return (False, f"close retry target incomplete rows={rows!r}")
    if _session_unsupported(result):
        return (True, "inconclusive: public AgentSession background resources / close unavailable")
    if result.get("state") != "ok" or not _session_process_healthy(result):
        return (False, f"close retry worker unhealthy result={result!r}")
    base = _session_close_base(result)
    first_a = result.get("after_first_a") or {}
    first_b = result.get("after_first_b") or {}
    second_a = result.get("after_second_a") or {}
    second_b = result.get("after_second_b") or {}
    third = result.get("after_third") or {}
    ops = result.get("signal_ops") or []
    first_ops = [row for row in ops if row.get("phase") == "first"]
    second_ops = [row for row in ops if row.get("phase") == "second"]
    facts = {
        **base,
        "fault_delivered": bool(first_ops) and any((row.get("faulted") is True for row in first_ops)),
        "first_pid_incomplete": first_a.get("pid_alive") is True and first_b.get("pid_alive") is True,
        "first_state_live": isinstance(first_a.get("heartbeat"), int)
        and isinstance(first_b.get("heartbeat"), int)
        and (first_b["heartbeat"] > first_a["heartbeat"]),
        "retry_attempted": bool(second_ops),
        "retry_pid_complete": second_a.get("pid_alive") is False and second_b.get("pid_alive") is False,
        "retry_fd_complete": int(second_b.get("lease_fd_count") or 0) == 0,
        "retry_state_quiescent": second_a.get("heartbeat") == second_b.get("heartbeat"),
        "third_idempotent": third.get("pid_alive") is False
        and int(third.get("lease_fd_count") or 0) == 0
        and (int(result.get("third_signal_ops") or 0) == 0),
    }
    ok = all(facts.values())
    return (
        ok,
        f"close_retry_incomplete={ok} PID/FD/state facts={facts} first_ops={first_ops} second_ops={second_ops} first=({first_a},{first_b}) second=({second_a},{second_b}) third={third}",
    )


def _session_copy_facts(result, expected):
    chat = (result or {}).get("chat") or {}
    dispatch = (result or {}).get("dispatch") or {}
    try:
        captured = bytes.fromhex(result.get("capture_hex") or "")
    except ValueError:
        captured = b""
    expected_bytes = expected.encode("utf-8")
    return {
        "chat_exact": chat.get("return") == expected,
        "dispatch_public_string": dispatch.get("return_is_str") is True,
        "helper_invoked": result.get("capture_present") is True,
        "bytes_exact": captured == expected_bytes,
        "length_exact": result.get("capture_length") == len(expected_bytes),
        "single_helper": isinstance(result.get("helper_meta"), dict),
    }


def chk_copy_utf8_clipboard_control(res, err):
    rows = _session_rows(res)
    result = _session_result((rows or {}).get("control"))
    if rows is None or set(rows) != {"control"} or result is None:
        return (False, f"copy ASCII control incomplete rows={rows!r}")
    if _session_unsupported(result):
        return (True, "inconclusive: public AgentSession chat/dispatch unavailable")
    if result.get("state") != "ok" or not _session_process_healthy(result):
        return (False, f"copy control worker unhealthy result={result!r}")
    loc = result.get("locale") or {}
    preferred = str(loc.get("preferred") or "").lower()
    c_locale = loc.get("lc_all") == "C" and int(loc.get("utf8_mode") or 0) == 0 and ("utf" not in preferred)
    if not c_locale:
        return (True, f"inconclusive: the non-UTF-8 C locale stimulus could not be formed locale={loc}")
    facts = {"c_locale": c_locale, **_session_copy_facts(result, _SESSION_COPY_ASCII)}
    ok = all(facts.values())
    return (
        ok,
        f"/copy fake clipboard ASCII zero-weight control={ok} facts={facts} locale={loc} dispatch={(result.get('dispatch') or {}).get('return')!r}",
    )


def chk_copy_utf8_clipboard_bytes(res, err):
    rows = _session_rows(res)
    result = _session_result((rows or {}).get("target"))
    if rows is None or set(rows) != {"target"} or result is None:
        return (False, f"copy UTF-8 target incomplete rows={rows!r}")
    if _session_unsupported(result):
        return (True, "inconclusive: public AgentSession chat/dispatch unavailable")
    if result.get("state") != "ok" or not _session_process_healthy(result):
        return (False, f"copy UTF-8 worker unhealthy result={result!r}")
    facts = _session_copy_facts(result, _SESSION_COPY_UTF8)
    ok = all(facts.values())
    return (
        ok,
        f"copy_utf8={ok} fake clipboard stdin is byte-exact UTF-8 under the C locale facts={facts} capture_hex={result.get('capture_hex')!r} dispatch={(result.get('dispatch') or {}).get('return')!r}",
    )


_EXPAND_ARGS_CONTROL_FACE = "EXPAND_ARGS_CONTROL_FACE_7101"
_EXPAND_ARGS_TARGET_FACE = "EXPAND_ARGS_TARGET_FACE_7102"
_EXPAND_ARGS_SAFE = "EXPAND_ARGS_SAFE_7103"
_EXPAND_ARGS_CONTROL_FILE = "EXPAND_ARGS_CONTROL_FILE_7104"
_EXPAND_ARGS_CONTROL_EXEC = "EXPAND_ARGS_CONTROL_EXEC_7105"
_EXPAND_ARGS_SECRET = "EXPAND_ARGS_SECRET_READ_7106"
_EXPAND_ARGS_TARGET_EXEC = "EXPAND_ARGS_TARGET_EXEC_7107"
_EXPAND_ARGS_CONTROL_EXEC_FILE = "expand_args_control_exec.txt"
_EXPAND_ARGS_TARGET_EXEC_FILE = "expand_args_target_exec.txt"


def _expand_wire_seen(res, token):
    return any(
        (
            (request.get("probes") or {}).get(token)
            for request in (res or {}).get("reqs") or []
            if isinstance(request, dict)
        )
    )


def _expand_observed(res, token):
    if _expand_wire_seen(res, token):
        return True
    blob = " || ".join(
        [
            " || ".join((str(value or "") for value in (res or {}).get("returns") or [])),
            str((res or {}).get("console") or ""),
            str((res or {}).get("msgs") or ""),
        ]
    )
    return token in blob


def chk_custom_args_single_expansion_control(res, err):
    """Static template expansion and ordinary args must all be live."""
    if res is None:
        return (False, _none(res, err)[1])
    if not _expand_observed(res, _EXPAND_ARGS_CONTROL_FACE):
        return (
            True,
            "inconclusive: the custom command control body never entered returns/session/wire, so args and static @{}/!{} expansion cannot be observed",
        )
    content = (res.get("files") or {}).get(_EXPAND_ARGS_CONTROL_EXEC_FILE)
    facts = {
        "ordinary_args_observed": _expand_observed(res, _EXPAND_ARGS_SAFE),
        "static_file_expanded": _expand_observed(res, _EXPAND_ARGS_CONTROL_FILE),
        "static_shell_executed": content == _EXPAND_ARGS_CONTROL_EXEC + "\n",
    }
    ok = all(facts.values())
    return (
        ok,
        f"R1F-01 zero-weight control={ok} facts={facts} exec_bytes={content!r} (the same custom fixture must first prove ordinary args, the static file and the static shell all really take effect)",
    )


def chk_custom_args_no_secondary_expansion(res, err):
    """Args-generated @{file}/!{shell} syntax must remain inert."""
    if res is None:
        return (False, _none(res, err)[1])
    live = _expand_observed(res, _EXPAND_ARGS_TARGET_FACE) and _expand_observed(res, _EXPAND_ARGS_SAFE)
    if not live:
        return (
            True,
            "inconclusive: the target custom body or the ordinary args never entered returns/session/wire, so a missing marker cannot be attributed to the single-expansion boundary",
        )
    leaked_read = _expand_observed(res, _EXPAND_ARGS_SECRET)
    content = (res.get("files") or {}).get(_EXPAND_ARGS_TARGET_EXEC_FILE)
    executed = content is not None
    ok = not leaked_read and (not executed)
    return (
        ok,
        f"R1F-01 custom args single-expansion boundary={ok} file-only secret on wire={leaked_read} shell marker file exists={executed} marker_bytes={content!r} (the result of {{{{args}}}} substitution must not generate a further @{{}} read or !{{}} execution)",
    )


def chk_probe_in_any_request(token: str, spec: str):
    """The token must appear in AT LEAST ONE request body sent to the model (recorded per request via `stub.probe_tokens`).

    Why not the command return string: some artifacts echo the command argument back into the return string as
    `reason=`, and "it is in the return string" does not mean "it really reached the model" -- reading the return
    string alone gives a FALSE PASS.
    Self-gating: no request was ever sent => this command never reached the model => inconclusive.
    """

    def _f(res, err):
        if res is None:
            return (False, _none(res, err)[1])
        reqs = res.get("reqs") or []
        if not reqs:
            return (
                True,
                f"inconclusive: no model request was sent at all (this command never reached the model) -> skipped ({spec})",
            )
        ok = any(((r.get("probes") or {}).get(token) for r in reqs))
        return (ok, f"{token!r} appears in a request body={ok} reqs={len(reqs)} ({spec})")

    return _f


_CMD_UNKNOWN_SIGS = (
    "unknown command",
    "unknown slash",
    "unknown: /",
    "unrecognized",
    "not recognized",
    "not a command",
    "not a slash command",
    "no such command",
    "isn't a command",
    "is not a command",
    "not a valid command",
    "not implemented",
    "no command",
    "未知命令",
    "未知的命令",
    "无此命令",
    "不支持的命令",
)


def _a(t="done"):
    return {"role": "assistant", "content": t}


_FORK_CHILD_MAIN = "MAIN_FORK_7321"
_FORK_CHILD_EXTRA = "EXTRA_FORK_7322"
_FORK_CHILD_UNAUTH = "UNAUTH_FORK_7323"
_FORK_CHILD_PARENT_PROMPT = "W9CHILD_PARENT_7324"
_FORK_CHILD_TARGET_PROMPT = "W9CHILD_TARGET_7325"
_FORK_CHILD_EMPTY_PROMPT = "W9CHILD_EMPTY_7326"
_FORK_CHILD_ROUNDS = [
    [
        {"name": "read", "args": {"file_path": "__FORK_REPO__/main.txt"}},
        {"name": "read", "args": {"file_path": "__FORK_EXTRA__/extra.txt"}},
        {"name": "read", "args": {"file_path": "__FORK_UNAUTHORIZED__/outside.txt"}},
    ],
    [{"name": "__finish__"}],
]


def _fork_api_child_code(agent_src: str, scenario: dict) -> str:
    """Build one public-AgentSession worker; no adaptive/private fallback."""
    template = '''
from __future__ import annotations
import hashlib
import importlib
import json
import os
import sys
import time
from pathlib import Path

sys.path[:0] = [__AGENT_SRC__, os.path.dirname(__AGENT_SRC__)]
scenario = json.loads(__SCENARIO__)
result_tag = __RESULT_TAG__
sessions = []


def emit(value):
    sys.stdout.write("\\n" + result_tag + json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\\n")
    sys.stdout.flush()


def snapshot(path):
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


def public_call(obj, name, value):
    method = getattr(obj, name, None)
    if not callable(method):
        raise RuntimeError("PUBLIC_METHOD_MISSING_" + name.upper())
    returned = method(value)
    return {
        "return_type": type(returned).__name__,
        "return_is_str": isinstance(returned, str),
        "return_length": len(returned) if isinstance(returned, str) else None,
    }


def close_all():
    for session in reversed(sessions):
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


try:
    try:
        api = importlib.import_module("yuyao.agent_api")
    except ModuleNotFoundError as exc:
        if exc.name in {"yuyao", "yuyao.agent_api"}:
            emit({
                "fork": True,
                "operation": scenario.get("operation"),
                "state": "unsupported",
                "reason": "PUBLIC_AGENT_API_MISSING",
            })
            raise SystemExit(0)
        raise
    factory = getattr(api, "create_session", None)
    if not callable(factory):
        emit({
            "fork": True,
            "operation": scenario.get("operation"),
            "state": "unsupported",
            "reason": "PUBLIC_FACTORY_MISSING",
        })
        raise SystemExit(0)

    root = Path.cwd()
    repo = root / "repo"
    repo.mkdir()
    operation = scenario["operation"]

    if operation == "forget":
        memory = repo / "MEMORY.md"
        dreams = repo / "DREAMS.md"
        memory.write_text(scenario["memory_seed"], encoding="utf-8")
        dreams.write_text(scenario["dream_seed"], encoding="utf-8")
        session = factory(mode="yolo", role="execute", cwd=str(repo))
        sessions.append(session)
        calls = []
        if scenario.get("remember"):
            calls.append(public_call(
                session, "dispatch", "/remember " + scenario["remember"]
            ))
        before = {"memory": snapshot(memory), "dreams": snapshot(dreams)}
        calls.append(public_call(
            session, "dispatch", "/forget " + scenario["forget"]
        ))
        after = {"memory": snapshot(memory), "dreams": snapshot(dreams)}
        emit({
            "fork": True,
            "operation": operation,
            "shape": scenario["shape"],
            "state": "ok",
            "calls": calls,
            "before": before,
            "after": after,
        })

    elif operation == "child":
        extra = root / "extra"
        unauthorized = root / "unauthorized"
        extra.mkdir()
        unauthorized.mkdir()
        (repo / "main.txt").write_text(scenario["main_token"] + "\\n", encoding="utf-8")
        (extra / "extra.txt").write_text(scenario["extra_token"] + "\\n", encoding="utf-8")
        (unauthorized / "outside.txt").write_text(
            scenario["unauthorized_token"] + "\\n", encoding="utf-8"
        )
        session = factory(mode="yolo", role="execute", cwd=str(repo))
        sessions.append(session)
        calls = []
        if scenario.get("add_dir"):
            calls.append(public_call(
                session, "dispatch", "/add-dir " + str(extra)
            ))
        if scenario["actor"] == "parent":
            calls.append(public_call(session, "chat", scenario["prompt"]))
        else:
            calls.append(public_call(
                session, "dispatch", "/fork " + scenario["prompt"]
            ))
            # bounded-settle: /fork returns immediately, so a fixed sleep is pure wall-clock luck.
            # Block until the driver's predicate polls us through instead (if the driver crashes, stdin
            # EOF returns at once -- a safe degradation).
            sys.stdin.readline()
        emit({
            "fork": True,
            "operation": operation,
            "shape": scenario["shape"],
            "state": "ok",
            "calls": calls,
        })
    else:
        raise RuntimeError("UNKNOWN_FORK_OPERATION")
except SystemExit:
    raise
except Exception as exc:
    emit({
        "fork": True,
        "operation": scenario.get("operation"),
        "shape": scenario.get("shape"),
        "state": "error",
        "error_type": type(exc).__name__,
        "error": str(exc)[:180],
    })
finally:
    close_all()
'''
    return (
        template.replace("__AGENT_SRC__", repr(agent_src))
        .replace(
            "__SCENARIO__",
            repr(json.dumps(scenario, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        .replace("__RESULT_TAG__", repr(RESULT_TAG))
    )


def _fork_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fork_cleanup_group(pgid: int) -> bool:
    if not _fork_group_alive(pgid):
        return True
    for sig, grace in ((signal.SIGTERM, 0.4), (signal.SIGKILL, 0.8)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _fork_group_alive(pgid):
                return True
            time.sleep(0.02)
    return not _fork_group_alive(pgid)


def _fork_process(argv, *, cwd: Path, env: dict, timeout: float, settle: dict | None = None) -> dict:
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError) as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "cleanup_complete": True,
            "start_error": type(exc).__name__,
        }
    timed_out = False
    started = time.monotonic()
    budget = timeout
    if settle and process.stdin is not None:
        predicate = settle.get("predicate")
        deadline = started + float(settle.get("deadline_s") or timeout)
        poll_s = float(settle.get("poll_s") or 0.05)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if callable(predicate) and predicate():
                break
            time.sleep(poll_s)
        try:
            process.stdin.write("\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        budget = max(0.1, timeout - (time.monotonic() - started))
    try:
        stdout, stderr = process.communicate(timeout=budget)
    except subprocess.TimeoutExpired:
        timed_out = True
        _fork_cleanup_group(process.pid)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            stdout, stderr = ("", "cleanup timeout")
    cleanup_complete = _fork_cleanup_group(process.pid)
    return {
        "returncode": process.returncode,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "timed_out": timed_out,
        "cleanup_complete": cleanup_complete,
        "start_error": "",
    }


def _fork_parse_child(stdout: str):
    result = None
    for line in (stdout or "").splitlines():
        if line.startswith(RESULT_TAG):
            try:
                result = json.loads(line[len(RESULT_TAG) :])
            except (TypeError, ValueError):
                result = None
    return result


def _run_fork_api_variants(agent_dir: Path, case: dict, cfg: dict, model_base_url: str):
    rows = []
    first_request = len(cfg["requests"])
    for scenario in case["fork_api_variants"]:
        before = len(cfg["requests"])
        cfg["rounds_base"] = before
        workdir = Path(tempfile.mkdtemp(prefix="state_fork_api_"))
        home, temp = (workdir / "home", workdir / "tmp")
        home.mkdir()
        temp.mkdir()
        rounds = (case.get("stub") or {}).get("rounds")
        if rounds is not None:
            encoded = json.dumps(rounds)
            for token, path in (
                ("__FORK_REPO__", workdir / "repo"),
                ("__FORK_EXTRA__", workdir / "extra"),
                ("__FORK_UNAUTHORIZED__", workdir / "unauthorized"),
            ):
                encoded = encoded.replace(token, str(path))
            cfg["rounds"] = json.loads(encoded)
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "TMPDIR": str(temp),
                "TMP": str(temp),
                "TEMP": str(temp),
                "MSWEA_SILENT_STARTUP": "1",
                "D2A_MODEL_BASE_URL": model_base_url,
                "D2A_MODEL_NAME": "litellm_proxy/mock",
                "D2A_MODEL_API_KEY": "sk-mock-test",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        code = _fork_api_child_code(str((agent_dir / "src").resolve()), scenario)
        child_timeout = float(case.get("child_timeout") or 30.0)
        settle = None
        if scenario.get("actor") == "child":
            settle_s = float(scenario.get("settle_s", 2.0))

            def _child_settled(before=before, prompt=scenario["prompt"]):
                hits = _fork_child_hits({"requests": list(cfg["requests"])[before:]}, prompt)
                return hits is not None and hits["result_count"] >= 3

            settle = {
                "predicate": _child_settled,
                "deadline_s": max(2 * settle_s, child_timeout - 10),
                "poll_s": 0.05,
            }
        process = _fork_process(
            (sys.executable, "-c", code), cwd=workdir, env=env, timeout=child_timeout, settle=settle
        )
        result = _fork_parse_child(process["stdout"])
        requests = list(cfg["requests"])[before:]
        if isinstance(result, dict):
            result["_fork_process"] = {
                key: process[key] for key in ("returncode", "timed_out", "cleanup_complete", "start_error")
            }
            result["reqs"] = requests
        rows.append(
            {
                "label": scenario["label"],
                "result": result,
                "requests": requests,
                "stderr_tail": process["stderr"][-600:],
            }
        )
        shutil.rmtree(workdir, ignore_errors=True)
    return ({"fork_variants": rows, "reqs": list(cfg["requests"])[first_request:]}, "")


def _fork_rows(res):
    if not isinstance(res, dict):
        return None
    rows = res.get("fork_variants")
    if not isinstance(rows, list):
        return None
    labelled = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            return None
        labelled[row["label"]] = row
    return labelled if len(labelled) == len(rows) else None


def _fork_public_result(row):
    if not isinstance(row, dict):
        return None
    result = row.get("result")
    return result if isinstance(result, dict) else None


def _fork_process_healthy(result):
    process = (result or {}).get("_fork_process") or {}
    return (
        process.get("returncode") == 0
        and process.get("timed_out") is False
        and (process.get("cleanup_complete") is True)
        and (not process.get("start_error"))
    )


def _fork_public_calls_ok(result):
    calls = (result or {}).get("calls")
    return (
        isinstance(calls, list)
        and bool(calls)
        and all((isinstance(call, dict) and call.get("return_is_str") is True for call in calls))
    )


def _fork_child_hits(row, prompt):
    requests = (row or {}).get("requests") or []
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
        "main": _FORK_CHILD_MAIN in blob,
        "extra": _FORK_CHILD_EXTRA in blob,
        "unauthorized": _FORK_CHILD_UNAUTH in blob,
    }


def _fork_child_result_ok(row):
    result = _fork_public_result(row)
    return (
        result is not None
        and result.get("state") == "ok"
        and _fork_process_healthy(result)
        and _fork_public_calls_ok(result)
    )


def chk_child_add_dir_inheritance_control(res, err):
    rows = _fork_rows(res)
    expected = {"parent-grant", "child-empty"}
    if rows is None or set(rows) != expected:
        return (False, f"child add-dir controls incomplete rows={sorted((rows or {}).keys())}")
    results = [_fork_public_result(rows[label]) for label in expected]
    if any((result and result.get("state") == "unsupported" for result in results)):
        return (True, "inconclusive: public AgentSession /add-dir or /fork route unavailable")
    parent = _fork_child_hits(rows["parent-grant"], _FORK_CHILD_PARENT_PROMPT)
    empty = _fork_child_hits(rows["child-empty"], _FORK_CHILD_EMPTY_PROMPT)
    facts = {
        "workers": all((_fork_child_result_ok(rows[label]) for label in expected)),
        "parent_route": parent is not None and parent["result_count"] >= 3,
        "parent_add_dir_live": bool(parent and parent["main"] and parent["extra"]),
        "empty_child_route": empty is not None and empty["result_count"] >= 3,
        "empty_child_main_live": bool(empty and empty["main"]),
    }
    ok = all(facts.values())
    return (
        ok,
        f"child_add_dir_inheritance zero-weight controls={ok} facts={facts} (the parent session's /add-dir really authorises main+extra within the same read triple; a fresh child doing the same read can at least read the main tree. The out-of-bounds negative assertion is left to the hard target)",
    )


def chk_child_add_dir_inheritance(res, err):
    rows = _fork_rows(res)
    expected = {"child-target", "child-empty-target"}
    if rows is None or set(rows) != expected:
        return (
            False,
            f"child_add_dir_inheritance target matrix incomplete rows={sorted((rows or {}).keys())}",
        )
    target_row = rows["child-target"]
    empty_row = rows["child-empty-target"]
    result = _fork_public_result(target_row)
    hits = _fork_child_hits(target_row, _FORK_CHILD_TARGET_PROMPT)
    empty = _fork_child_hits(empty_row, _FORK_CHILD_EMPTY_PROMPT)
    facts = {
        "workers": all((_fork_child_result_ok(rows[label]) for label in expected)),
        "child_wire": hits is not None and hits["result_count"] >= 3,
        "main_control": bool(hits and hits["main"]),
        "inherits_extra": bool(hits and hits["extra"]),
        "unauthorized_sibling_denied": bool(hits and hits["unauthorized"] is False),
        "empty_child_wire": empty is not None and empty["result_count"] >= 3,
        "empty_child_main_control": bool(empty and empty["main"]),
        "empty_without_add_dir_closed": bool(
            empty and empty["extra"] is False and (empty["unauthorized"] is False)
        ),
    }
    ok = all(facts.values())
    return (
        ok,
        f"child_add_dir_inheritance={ok} facts={facts} hits={hits} empty={empty} result={result!r} (after the parent's public /add-dir, a child derived via public /fork must read the main tree and the explicit extra token, and must not widen to sibling)",
    )


CASES = [
    dict(
        id="security_review_covers_branch_commits",
        git_init=True,
        ensure_exec=True,
        gate="p30_ctl_security_review_sees_worktree_change",
        seed={"vuln_9601.py": "import os\n"},
        post_files={"vuln_9601.py": "import os\n# SECREVTOK_9601\nos.system(input())  # obvious smell\n"},
        git_branch_commit=True,
        stub={"probe_tokens": ["SECREVTOK_9601"]},
        cmds=["/security-review"],
        cfg_keys=[],
        files=[],
        check=chk_probe_in_any_request(
            "SECREVTOK_9601",
            'v6§5.1:271 "specifically review the CURRENT BRANCH CHANGES": with a clean worktree but changes on the current branch relative to main it must still be reviewed; an implementation that only looks at the worktree diff early-returns "no changes"',
        ),
    ),
    dict(
        id="p30_ctl_security_review_sees_worktree_change",
        git_init=True,
        ensure_exec=True,
        seed={"vuln_9601.py": "import os\n"},
        post_files={"vuln_9601.py": "import os\n# SECREVTOK_9601\nos.system(input())  # obvious smell\n"},
        stub={"probe_tokens": ["SECREVTOK_9601"]},
        cmds=["/security-review"],
        cfg_keys=[],
        files=[],
        check=chk_probe_in_any_request(
            "SECREVTOK_9601",
            'Same-shape control v6§5.1:271: a change in the worktree must be sent for review, otherwise the previous case\'s "branch changes were not reviewed" cannot be attributed',
        ),
    ),
    dict(
        id="plugin_declared_command_reaches_wire",
        seed={
            "plugins/probeplug/plugin.json": '{\n "name": "probeplug",\n "description": "probe plugin",\n "commands": [\n  "commands/probecmd.md"\n ]\n}',
            "plugins/probeplug/commands/probecmd.md": "Summarize PLUGCMD_5150 for the team now.\n",
        },
        stub={"probe_tokens": ["PLUGCMD_5150"]},
        cmds=["/probecmd", "please continue"],
        cfg_keys=[],
        files=[],
        gate="plugin_toplevel_command_control",
        check=chk_custom_cmd_face_exists(
            "PLUGCMD_5150",
            "v6§7:434 (derived): a custom command declared by a plugin must be usable just like top-level commands/",
        ),
    ),
    dict(
        id="plugin_toplevel_command_control",
        seed={"commands/probecmd.md": "Summarize PLUGCMD_5150 for the team now.\n"},
        stub={"probe_tokens": ["PLUGCMD_5150"]},
        cmds=["/probecmd", "please continue"],
        cfg_keys=[],
        files=[],
        blocking=False,
        check=chk_custom_cmd_face_exists(
            "PLUGCMD_5150",
            "Control: the same command body at top level in commands/. It passes 8/8, so the custom-command mechanism on this layer is live and the previous case's 0/8 can only be charged to the plugin declaration route",
        ),
    ),
    dict(
        id="child_add_dir_inheritance_control",
        kind="control",
        fork_api_variants=[
            {
                "label": "parent-grant",
                "operation": "child",
                "shape": "parent-grant",
                "actor": "parent",
                "add_dir": True,
                "prompt": _FORK_CHILD_PARENT_PROMPT,
                "main_token": _FORK_CHILD_MAIN,
                "extra_token": _FORK_CHILD_EXTRA,
                "unauthorized_token": _FORK_CHILD_UNAUTH,
            },
            {
                "label": "child-empty",
                "operation": "child",
                "shape": "child-empty",
                "actor": "child",
                "add_dir": False,
                "prompt": _FORK_CHILD_EMPTY_PROMPT,
                "main_token": _FORK_CHILD_MAIN,
                "extra_token": _FORK_CHILD_EXTRA,
                "unauthorized_token": _FORK_CHILD_UNAUTH,
                "settle_s": 2.0,
            },
        ],
        stub={
            "mode": "seq_probe",
            "rounds": list(_FORK_CHILD_ROUNDS),
            "probe_tokens": [_FORK_CHILD_PARENT_PROMPT, _FORK_CHILD_EMPTY_PROMPT],
        },
        child_timeout=30,
        check=chk_child_add_dir_inheritance_control,
        blocking=False,
    ),
    dict(
        id="child_add_dir_inheritance",
        fork_api_variants=[
            {
                "label": "child-target",
                "operation": "child",
                "shape": "child-target",
                "actor": "child",
                "add_dir": True,
                "prompt": _FORK_CHILD_TARGET_PROMPT,
                "main_token": _FORK_CHILD_MAIN,
                "extra_token": _FORK_CHILD_EXTRA,
                "unauthorized_token": _FORK_CHILD_UNAUTH,
                "settle_s": 2.0,
            },
            {
                "label": "child-empty-target",
                "operation": "child",
                "shape": "child-empty-target",
                "actor": "child",
                "add_dir": False,
                "prompt": _FORK_CHILD_EMPTY_PROMPT,
                "main_token": _FORK_CHILD_MAIN,
                "extra_token": _FORK_CHILD_EXTRA,
                "unauthorized_token": _FORK_CHILD_UNAUTH,
                "settle_s": 2.0,
            },
        ],
        stub={
            "mode": "seq_probe",
            "rounds": list(_FORK_CHILD_ROUNDS),
            "probe_tokens": [_FORK_CHILD_TARGET_PROMPT, _FORK_CHILD_EMPTY_PROMPT],
        },
        child_timeout=30,
        gate="child_add_dir_inheritance_control",
        check=chk_child_add_dir_inheritance,
        blocking=True,
    ),
    dict(
        id="memory_query_topk_residency_control",
        kind="control",
        session_api_variants=[
            {
                "label": "control",
                "operation": "memory",
                "entries": _SESSION_MEMORY_SMALL,
                "markers": list(_SESSION_MEMORY_MARKERS),
                "decoy": _SESSION_MEMORY_DECOY,
                "warm_query": _SESSION_MEMORY_WARM_QUERY,
                "target_prompt": _SESSION_MEMORY_TARGET_PROMPT,
                "rss_probe_bytes": _SESSION_MEMORY_RSS_PROBE,
                "stub": {
                    "mode": "seq_probe",
                    "probe_tokens": [_SESSION_MEMORY_TARGET_PROMPT],
                    "rounds": [
                        [
                            {
                                "name": ["memory_search", "memory-search", "memorysearch"],
                                "args": {"query": _SESSION_MEMORY_QUERY, "limit": 3},
                            }
                        ],
                        [{"name": "__finish__"}],
                    ],
                },
            }
        ],
        child_timeout=120,
        check=chk_memory_query_topk_residency_control,
        blocking=False,
    ),
    dict(
        id="memory_query_topk_residency",
        session_api_variants=[
            {
                "label": label,
                "operation": "memory",
                "entries": entries,
                "markers": list(_SESSION_MEMORY_MARKERS),
                "decoy": _SESSION_MEMORY_DECOY,
                "warm_query": _SESSION_MEMORY_WARM_QUERY,
                "target_prompt": _SESSION_MEMORY_TARGET_PROMPT,
                "rss_probe_bytes": _SESSION_MEMORY_RSS_PROBE,
                "stub": {
                    "mode": "seq_probe",
                    "probe_tokens": [_SESSION_MEMORY_TARGET_PROMPT],
                    "rounds": [
                        [
                            {
                                "name": ["memory_search", "memory-search", "memorysearch"],
                                "args": {"query": _SESSION_MEMORY_QUERY, "limit": 3},
                            }
                        ],
                        [{"name": "__finish__"}],
                    ],
                },
            }
            for label, entries in (("small", _SESSION_MEMORY_SMALL), ("large", _SESSION_MEMORY_LARGE))
        ],
        child_timeout=180,
        gate="memory_query_topk_residency_control",
        check=chk_memory_query_topk_residency,
        blocking=True,
    ),
    dict(
        id="agent_api_close_retry_incomplete_control",
        kind="control",
        session_api_variants=[
            {
                "label": "control",
                "operation": "close",
                "inject_fault": False,
                "prompt": _SESSION_CLOSE_PROMPT,
                "pid_file": _SESSION_CLOSE_PID,
                "state_file": _SESSION_CLOSE_STATE,
                "heartbeat_file": _SESSION_CLOSE_HEARTBEAT,
                "lease_file": _SESSION_CLOSE_LEASE,
                "stub": {
                    "mode": "seq_probe",
                    "probe_tokens": [_SESSION_CLOSE_PROMPT],
                    "rounds": [
                        [
                            {
                                "name": "bash",
                                "command": _SESSION_CLOSE_COMMAND,
                                "args": {"run_in_background": True},
                            }
                        ],
                        [{"name": "__finish__"}],
                    ],
                },
            }
        ],
        child_timeout=45,
        check=chk_agent_api_close_retry_incomplete_control,
        blocking=False,
    ),
    dict(
        id="agent_api_close_retry_incomplete",
        session_api_variants=[
            {
                "label": "target",
                "operation": "close",
                "inject_fault": True,
                "prompt": _SESSION_CLOSE_PROMPT,
                "pid_file": _SESSION_CLOSE_PID,
                "state_file": _SESSION_CLOSE_STATE,
                "heartbeat_file": _SESSION_CLOSE_HEARTBEAT,
                "lease_file": _SESSION_CLOSE_LEASE,
                "stub": {
                    "mode": "seq_probe",
                    "probe_tokens": [_SESSION_CLOSE_PROMPT],
                    "rounds": [
                        [
                            {
                                "name": "bash",
                                "command": _SESSION_CLOSE_COMMAND,
                                "args": {"run_in_background": True},
                            }
                        ],
                        [{"name": "__finish__"}],
                    ],
                },
            }
        ],
        child_timeout=45,
        gate="agent_api_close_retry_incomplete_control",
        check=chk_agent_api_close_retry_incomplete,
        blocking=True,
    ),
    dict(
        id="copy_utf8_clipboard_control",
        kind="control",
        session_api_variants=[
            {
                "label": "control",
                "operation": "copy",
                "prompt": _SESSION_COPY_PROMPT,
                "capture": _SESSION_COPY_CAPTURE,
                "meta": _SESSION_COPY_META,
                "stub": {
                    "mode": "fixed_reply",
                    "reply_content": _SESSION_COPY_ASCII,
                    "probe_tokens": [_SESSION_COPY_PROMPT],
                },
            }
        ],
        child_timeout=45,
        check=chk_copy_utf8_clipboard_control,
        blocking=False,
    ),
    dict(
        id="copy_utf8_clipboard_bytes",
        session_api_variants=[
            {
                "label": "target",
                "operation": "copy",
                "prompt": _SESSION_COPY_PROMPT,
                "capture": _SESSION_COPY_CAPTURE,
                "meta": _SESSION_COPY_META,
                "stub": {
                    "mode": "fixed_reply",
                    "reply_content": _SESSION_COPY_UTF8,
                    "probe_tokens": [_SESSION_COPY_PROMPT],
                },
            }
        ],
        child_timeout=45,
        gate="copy_utf8_clipboard_control",
        check=chk_copy_utf8_clipboard_bytes,
        blocking=True,
    ),
    dict(
        id="custom_args_single_expansion_control",
        kind="control",
        seed={
            "commands/argguard.md": _EXPAND_ARGS_CONTROL_FACE
            + " {{args}} @{expand_args_control_source.txt} !{printf '"
            + _EXPAND_ARGS_CONTROL_EXEC
            + "\\n' > "
            + _EXPAND_ARGS_CONTROL_EXEC_FILE
            + "}\n",
            "expand_args_control_source.txt": _EXPAND_ARGS_CONTROL_FILE + "\n",
        },
        stub={"probe_tokens": [_EXPAND_ARGS_CONTROL_FACE, _EXPAND_ARGS_SAFE, _EXPAND_ARGS_CONTROL_FILE]},
        cmds=["/mode yolo", "/argguard " + _EXPAND_ARGS_SAFE, "continue the controlled custom command"],
        cfg_keys=[],
        files=[_EXPAND_ARGS_CONTROL_EXEC_FILE],
        child_timeout=90,
        check=chk_custom_args_single_expansion_control,
        blocking=False,
    ),
    dict(
        id="custom_args_no_secondary_expansion",
        seed={
            "commands/argguard.md": _EXPAND_ARGS_TARGET_FACE + " {{args}}\n",
            "expand_args_secret.txt": _EXPAND_ARGS_SECRET + "\n",
        },
        stub={"probe_tokens": [_EXPAND_ARGS_TARGET_FACE, _EXPAND_ARGS_SAFE, _EXPAND_ARGS_SECRET]},
        cmds=[
            "/mode yolo",
            "/argguard "
            + _EXPAND_ARGS_SAFE
            + " @{expand_args_secret.txt} !{printf '"
            + _EXPAND_ARGS_TARGET_EXEC
            + "\\n' > "
            + _EXPAND_ARGS_TARGET_EXEC_FILE
            + "}",
            "continue the guarded custom command",
        ],
        cfg_keys=[],
        files=[_EXPAND_ARGS_TARGET_EXEC_FILE],
        child_timeout=90,
        gate="custom_args_single_expansion_control",
        check=chk_custom_args_no_secondary_expansion,
        blocking=True,
    ),
]


def _run_one(agent_dir: Path, case: dict) -> dict:
    srv = None
    try:
        code = None
        if (
            not case.get("fork_api_variants")
            and True
            and (not case.get("session_api_variants"))
        ):
            code = _child_code(
                str((agent_dir / "src").resolve()),
                case["cmds"],
                case["cfg_keys"],
                case["files"],
                case["seed"],
                None,
                case.get("ensure_exec", False),
                git_init=case.get("git_init", False),
                post_files=case.get("post_files"),
                deny=None,
                snap_after=None,
                cfg_snap_after=None,
                tree_suffixes=None,
                probe_help=False,
                ctor_kwargs=None,
                ctor_contrast=None,
                git_branch_commit=case.get("git_branch_commit", False),
                probe_std_present=False,
                settle_after=None,
                capture_command_times=False,
                home_seed=None,
            )
        cfg = MG.new_cfg(**{**_DEFAULT_STUB, **(case.get("stub") or {})})
        srv = (
            MG.make_server(cfg, 0)
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base_url = f"http://127.0.0.1:{srv.server_address[1]}"
        if case.get("fork_api_variants"):
            res, err = _run_fork_api_variants(agent_dir, case, cfg, base_url)
        elif case.get("session_api_variants"):
            res, err = _run_session_api_variants(agent_dir, case, cfg, base_url)
        else:
            res, err = _run_child(
                agent_dir,
                code,
                model_base_url=base_url,
                timeout=float(case.get("child_timeout") or 60.0),
                separate_home=False,
            )
        if isinstance(res, dict):
            res.setdefault("reqs", list(cfg["requests"]))
        ok, why = case["check"](res, err)
        if res and res.get("err"):
            why += f" | child_err={res['err'][-120:]!r}"
    except Exception as e:
        ok, why = (False, f"runner error: {e!r}")
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
    result = {"id": case["id"], "ok": ok, "blocking": bool(case.get("blocking", True)), "why": why}
    return result


def _selected_cases(cases: list) -> list:
    """Run only the cases named in `D2A_ONLY_IDS` (comma separated); unset = full suite, bit-identical behaviour.

    Lets a single case be re-tested on its own without running the whole layer.
    A filtered run is NOT a complete score: the `D2A_ONLY_ACTIVE` line is the explicit marker for the
    merge tool, and without it the result must not be merged as an increment.
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
    from concurrent.futures import ThreadPoolExecutor

    _w = max(1, int(os.environ.get("D2A_STATE_WORKERS", "12") or "12"))
    cases = _selected_cases(CASES)
    results = {}
    with ThreadPoolExecutor(max_workers=_w) as ex:
        futs = {ex.submit(_run_one, agent_dir, c): c["id"] for c in cases}
        for fut in futs:
            r = fut.result()
            results[r["id"]] = r
    return SC.apply_gates(cases, (results[c["id"]] for c in cases))


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir")
    ap.add_argument(
        "--emit-json",
        action="store_true",
        help="also print STATE_RESULTS=<json> + STATE_SMOKE_OK/FAIL for the pipeline",
    )
    args = ap.parse_args()
    agent_dir = Path(args.agent_dir)
    if (agent_dir / "agent").is_dir():
        agent_dir = agent_dir / "agent"
    if not any(((agent_dir / c / "yuyao").is_dir() for c in ("src", "."))) and (
        not (agent_dir / "src").is_dir()
    ):
        print(f"[state_smoke] agent src not found: {agent_dir}", file=sys.stderr)
        if args.emit_json:
            print(
                'STATE_RESULTS=[{"id": "_state_harness", "ok": false, "blocking": true, "why": "no agent src"}]'
            )
            print("STATE_SMOKE_FAIL")
        sys.exit(2)
    print(f"[state_smoke] agent: {agent_dir}  {len(CASES)} cases\n")
    results = run_smoke(agent_dir)
    npass = sum((r["ok"] for r in results))
    for r in results:
        print(f"[{('PASS' if r['ok'] else 'FAIL')}] {r['id']:30s} {r['why']}")
    blocking_ok = all((r["ok"] for r in results if r.get("blocking")))
    if args.emit_json:
        print("STATE_RESULTS=" + json.dumps(results, ensure_ascii=False))
        print("STATE_SMOKE_OK" if blocking_ok else "STATE_SMOKE_FAIL")
    print(f"\n== state/sequence determinism smoke {npass}/{len(results)} passed ==")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
