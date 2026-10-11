"""Fault-injection mock gateway (OpenAI-compatible, stdlib only, no deps).

With the agent's `D2A_MODEL_BASE_URL` pointed here, this stands in for the model
and behaves deterministically:
  - normal mode returns a tool_call that drives the agent to run bash (no real model spend);
  - other MODEs inject real-world faults (null usage, missing fields, bad JSON arguments,
    plain text with no tool call, ...) to probe agent robustness. Real models cannot
    reproduce these reliably.
Every request is recorded (message count, null bytes, Authorization, model name, ...) for
test assertions.

litellm (`litellm_proxy/<name>`) POSTs to `{api_base}/chat/completions`, so any POST ending
in `chat/completions` is answered; GET always returns 200 with an empty object (tolerates
occasional /models probes).

Standalone: python mock_gateway.py --port 9009 --mode usage_null
Or import it: switch modes via CONFIG['mode'], read records from CONFIG['requests'].
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG: dict = {
    "mode": os.environ.get("MOCK_MODE", "normal"),
    "command": os.environ.get("MOCK_COMMAND", "echo MOCK_PROOF_7731 > mock_proof.txt"),
    "usage_tokens": (12, 8),
    "reject_substr": "",
    "strict": False,
    "requests": [],
}


def new_cfg(**over) -> dict:
    """Build an independent cfg (one per server so concurrent runs do not interfere)."""
    c = {
        "mode": "normal",
        "command": "echo MOCK_PROOF_7731 > mock_proof.txt",
        "usage_tokens": (12, 8),
        "reject_substr": "",
        "strict": False,
        "allow_cmd": "echo PERM_LIVE_7755 > live.txt",
        "deny_cmd": "touch PERM_DENY_7755.txt",
        "probe2_cmd": "",
        "probe_tool": "bash",
        "probe_args": None,
        "err_before": 2,
        "err_status": 429,
        "rounds": None,
        "rounds_base": 0,
        "probe_token": "",
        "probe_tokens": [],
        "target_command": "",
        "target_left": "",
        "target_right": "",
        "structured_prompt_token": "",
        "structured_prompt_end": "",
        "structured_prompt_expected": "",
        "structured_prompt_fragment": "",
        "reply_content": "",
        "web_gets": [],
        "requests": [],
    }
    c.update(over)
    return c


_USTR_RE = re.compile("\\\\u([0-9a-fA-F]{4})")


def _has_lone_surrogate(text: str) -> bool:
    """A legal emoji in JSON is a high+low pair `\\ud83d\\ude00`; an unpaired high/low surrogate
    is illegal (real OpenAI/Anthropic gateways 400 on lone surrogates, so a correct agent should
    sanitize before sending)."""
    toks = [(m.start(), int(m.group(1), 16)) for m in _USTR_RE.finditer(text)]
    n, i = (len(toks), 0)
    while i < n:
        pos, v = toks[i]
        if 55296 <= v <= 56319:
            if i + 1 < n:
                npos, nv = toks[i + 1]
                if npos == pos + 6 and 56320 <= nv <= 57343:
                    i += 2
                    continue
            return True
        if 56320 <= v <= 57343:
            return True
        i += 1
    return False


def _tool_names(req: dict) -> list:
    """Function names in the request's tools array (OpenAI function-calling schema). Empty/malformed -> []."""
    out = []
    for t in req.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else None
        name = (fn or {}).get("name") if fn else t.get("name")
        if name:
            out.append(str(name))
    return out


_PARAM_SYNONYMS = [
    (
        "path",
        "file_path",
        "filepath",
        "filePath",
        "filename",
        "file",
        "target",
        "target_file",
        "fname",
        "file_name",
        "notebook_path",
    ),
    (
        "content",
        "contents",
        "text",
        "data",
        "body",
        "new_content",
        "file_text",
        "file_content",
        "source",
        "new_source",
    ),
    ("old_string", "oldString", "old", "old_str", "search", "find", "from", "old_text"),
    ("new_string", "newString", "new", "new_str", "replace", "replacement", "to", "new_text"),
    ("replace_all", "replaceAll"),
    ("cell_index", "cell_number", "cell"),
    ("mode", "edit_mode"),
    ("background", "run_in_background", "run_background", "is_background", "bg"),
    ("job_id", "id", "job", "bg_id", "process_id"),
    ("url", "uri", "href", "link"),
    ("agent", "name", "subagent", "agent_name", "profile", "subagent_type"),
]
_TOOL_NAME_SYNONYMS = [
    {"write", "write_file", "create_file", "file_write", "writefile", "put_file", "save_file"},
    {"read", "read_file", "file_read", "readfile", "cat", "view_file", "open_file"},
    {"edit", "edit_file", "str_replace", "replace_in_file", "apply_edit", "modify_file"},
    {"multi_edit", "multi-edit", "multiedit", "batch_edit", "edit_many"},
    {"bash", "shell", "run_command", "execute", "exec", "terminal"},
    {"glob", "find_files", "list_files", "file_search"},
    {"grep", "search", "ripgrep", "search_files", "grep_search"},
    {"bg_list", "bg-list", "bglist"},
    {"bg_output", "bg-output", "bgoutput"},
    {"bg_stop", "bg-stop", "bgstop"},
    {"web_fetch", "web-fetch", "webfetch", "WebFetch"},
    {"web_search", "web-search", "websearch", "WebSearch"},
]
_CANON = {
    "bash",
    "read",
    "write",
    "edit",
    "multi_edit",
    "apply_patch",
    "grep",
    "glob",
    "web_fetch",
    "web_search",
    "browser",
    "read_media",
    "lsp",
    "notebook_edit",
    "monitor",
    "todo",
    "task",
    "bg_list",
    "bg_output",
    "bg_stop",
    "memory_search",
    "memory_get",
    "ask_user",
    "submit",
}
_TOOL_ALIASES = {
    "shell": "bash",
    "run_command": "bash",
    "run_shell": "bash",
    "execute_command": "bash",
    "terminal": "bash",
    "exec": "bash",
    "execute": "bash",
    "cat": "read",
    "view_file": "read",
    "open_file": "read",
    "file_read": "read",
    "create_file": "write",
    "file_write": "write",
    "put_file": "write",
    "save_file": "write",
    "new_file": "write",
    "str_replace": "edit",
    "str_replace_editor": "edit",
    "replace_in_file": "edit",
    "apply_edit": "edit",
    "modify_file": "edit",
    "search_replace": "edit",
    "batch_edit": "multi_edit",
    "edit_many": "multi_edit",
    "multiedit": "multi_edit",
    "ripgrep": "grep",
    "rg": "grep",
    "grep_search": "grep",
    "content_search": "grep",
    "search_files": "grep",
    "find_files": "glob",
    "list_files": "glob",
    "file_glob": "glob",
    "file_search": "glob",
    "patch": "apply_patch",
    "apply_diff": "apply_patch",
    "todos": "todo",
    "todo_add": "todo",
    "todo_update": "todo",
    "todo_write": "todo",
    "todo_list": "todo",
    "task_list": "todo",
    "tasks": "todo",
    "subagent": "task",
    "spawn_agent": "task",
    "task_subagent": "task",
}
_TOOL_STRIP_SUFFIX = ("_file", "_files", "_tool", "_cmd", "_command", "_op")
_TOOL_STRIP_PREFIX = ("tool_", "fn_", "do_", "builtin_")
_BRAND = "yuyao"


def _mech_norm(name: str) -> str:
    """Mechanical normalization: lowercase, unify separators, fold repeated underscores, trim them."""
    s = str(name).strip().lower()
    for ch in ("-", ".", " ", "/", ":"):
        s = s.replace(ch, "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def canon_tool(name: str) -> str | None:
    """Map a build-advertised tool name to the canonical name of its spec §1 capability family;
    return None when undecidable (never guess).

    Normalization resolves the wire tool names products use in practice without merging distinct
    capabilities: names such as read_only/edit_config/write_log/grep_history/globals/grepper/no_op/
    edit_distance/ls stay unmapped.
    """
    if not name or not isinstance(name, str):
        return None
    s = _mech_norm(name)
    if s in _CANON:
        return s
    b = _mech_norm(_BRAND)
    if s.startswith(f"{b}_"):
        t = s[len(b) + 1 :]
        if t in _CANON:
            return t
        if t in _TOOL_ALIASES:
            return _TOOL_ALIASES[t]
        for suf in _TOOL_STRIP_SUFFIX:
            if t.endswith(suf) and t[: -len(suf)] in _CANON:
                return t[: -len(suf)]
    if s in _TOOL_ALIASES:
        return _TOOL_ALIASES[s]
    for suf in _TOOL_STRIP_SUFFIX:
        if s.endswith(suf):
            t = s[: -len(suf)]
            if t in _CANON:
                return t
            if t in _TOOL_ALIASES:
                return _TOOL_ALIASES[t]
    for pre in _TOOL_STRIP_PREFIX:
        if s.startswith(pre):
            t = s[len(pre) :]
            if t in _CANON:
                return t
            if t in _TOOL_ALIASES:
                return _TOOL_ALIASES[t]
    return None


def adv_has_tool(adv, wanted) -> bool:
    """Sole judging-side entry point: does the build's advertised list `adv` contain the CAPABILITY
    `wanted`?

    `wanted` may be a single name or a sequence (any hit counts). Both sides go through
    `canon_tool`, so spellings like `yuyao_edit` / `edit_file` / `multi-edit` are not
    misjudged as "not exposed". Names that do not normalize (including ambiguous ones) count as no
    match - better undecidable than a false PASS.
    """
    wants = [wanted] if isinstance(wanted, str) else list(wanted or ())
    want_caps = {c for c in (canon_tool(w) for w in wants) if c}
    if not want_caps:
        return False
    adv_caps = {c for c in (canon_tool(a) for a in adv or ()) if c}
    return bool(want_caps & adv_caps)


def _content_as_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [p.get("text", "") for p in c if isinstance(p, dict) and isinstance(p.get("text"), str)]
        return "\n".join(parts)
    return ""


def _structured_prompt_facts(req: dict, token: str, end_sentinel: str, expected: str, fragment: str) -> dict:
    """Observe the bounded task span in the first user message, never its wrapper."""
    messages = req.get("messages") or []
    first_user = next((m for m in messages if isinstance(m, dict) and m.get("role") == "user"), {})
    text = _content_as_text(first_user.get("content"))
    non_user_blob = "\n".join(
        (
            _content_as_text(m.get("content"))
            for m in messages
            if isinstance(m, dict) and m.get("role") != "user"
        )
    )
    starts = [match.start() for match in re.finditer(re.escape(token), text)] if token else []
    spans = []
    for start in starts:
        end = text.find(end_sentinel, start + len(token)) if end_sentinel else -1
        if end >= 0:
            spans.append(text[start : end + len(end_sentinel)])
    one_span = spans[0] if len(spans) == 1 else ""
    if not one_span:
        wrapper_category = "unbounded"
    else:
        start = text.find(one_span)
        has_prefix = start > 0
        has_suffix = start + len(one_span) < len(text)
        wrapper_category = (
            "prefix-and-suffix"
            if has_prefix and has_suffix
            else "prefix" if has_prefix else "suffix" if has_suffix else "none"
        )
    return {
        "first_user_role": first_user.get("role"),
        "task_span_count": len(spans),
        "task_span_chars": len(one_span),
        "task_span_sha256": hashlib.sha256(one_span.encode()).hexdigest() if one_span else "",
        "task_span_matches_expected": bool(one_span) and one_span == expected,
        "wrapper_category": wrapper_category,
        "complete_token": token if token and token in text else "",
        "structured_fragment_seen": bool(fragment) and fragment in text,
        "token_only_outside_user": bool(token) and token not in text and (token in non_user_blob),
    }


def _latest_tool_batch(req: dict) -> list:
    """Take only the MOST RECENT round of tool result texts.

    Standard OpenAI ordering is `assistant(tool_calls) -> tool -> ...`. A few builds omit the
    preceding assistant and emit `tool -> assistant(next tool_calls)`; accepting only "tool after
    assistant" would miss the whole batch. Both orderings are accepted.
    """
    msgs = req.get("messages") or []
    last_tc = None
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls"):
            last_tc = i
    if last_tc is not None:
        out = []
        for m in msgs[last_tc + 1 :]:
            if not isinstance(m, dict) or m.get("role") != "tool":
                break
            t = _content_as_text(m.get("content"))
            if t:
                out.append(t)
        if out:
            return out
    last_asst = None
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            last_asst = i
    if last_asst is None:
        return []
    batch = []
    j = last_asst - 1
    while j >= 0 and isinstance(msgs[j], dict) and (msgs[j].get("role") == "tool"):
        t = _content_as_text(msgs[j].get("content"))
        if t:
            batch.append(t)
        j -= 1
    batch.reverse()
    return batch


def _target_exchange_facts(req: dict, command: str, left: str, right: str) -> dict:
    """Bind one configured bash call to its matching tool result."""
    facts = {
        "target_call_seen": False,
        "target_result_seen": False,
        "target_result_has_left": False,
        "target_result_has_right": False,
    }
    if not command:
        return facts
    messages = req.get("messages") or []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if not isinstance(function, dict) or function.get("name") != "bash":
                continue
            try:
                arguments = function.get("arguments") or "{}"
                args = arguments if isinstance(arguments, dict) else json.loads(arguments)
            except Exception:
                continue
            if args.get("command") != command:
                continue
            facts["target_call_seen"] = True
            call_id = call.get("id")
            for result in messages[index + 1 :]:
                if not isinstance(result, dict):
                    continue
                if result.get("role") == "assistant":
                    break
                if result.get("role") != "tool":
                    continue
                if call_id and result.get("tool_call_id") != call_id:
                    continue
                text = _content_as_text(result.get("content"))
                facts.update(
                    {
                        "target_result_seen": True,
                        "target_result_has_left": bool(left) and left in text,
                        "target_result_has_right": bool(right) and right in text,
                    }
                )
                return facts
    return facts


_BG_ID_BLOCKLIST = {
    "bg-list",
    "bg_list",
    "bglist",
    "bg-output",
    "bg_output",
    "bgoutput",
    "bg-stop",
    "bg_stop",
    "bgstop",
}


def _extract_bg_job_id(texts) -> str | None:
    """Pull a background job id out of earlier tool_result text. Id shapes vary by build
    (bg-XXXX / bare digits / [n]).

    The regexes must stay tight: too broad and the `s` of `no jobs`, or the tool name `bg-output`,
    gets mistaken for an id.
    """
    blob = "\n".join(texts or [])
    if not blob.strip():
        return None
    if re.search(
        "(?i)\\bno\\s+(?:\\w+\\s+){0,2}(?:background\\s+)?(?:jobs?|processes|tasks)\\b", blob
    ) or re.search("(?i)\\((?:none|empty)\\)", blob):
        return None
    m = re.search("\\b(?:job|process)\\s+#(\\d+)\\b", blob, re.I)
    if m:
        return m.group(1)
    m = re.search(
        "(?:started\\s+)?background\\s+(?:job|process|task)s?\\s*[#:]?\\s*([A-Za-z0-9_-]+)\\b", blob, re.I
    )
    if (
        m
        and m.group(1).lower() not in _BG_ID_BLOCKLIST
        and (m.group(1).lower() not in {"id", "job", "process", "task"})
    ):
        return m.group(1)
    m = re.search("\\b(bg[-_][A-Za-z0-9][A-Za-z0-9_-]*)\\b", blob)
    if m and m.group(1).lower() not in _BG_ID_BLOCKLIST:
        return m.group(1)
    m = re.search("\\bjob(?:_id|\\s+id)\\s*[:=]\\s*([A-Za-z0-9_-]+)\\b", blob, re.I)
    if m:
        return m.group(1)
    m = re.search("(?m)^\\s*#([A-Za-z0-9_-]+)\\b", blob)
    if m and m.group(1).lower() not in _BG_ID_BLOCKLIST:
        return m.group(1)
    m = re.search("\\[(\\d+)\\]", blob)
    if m:
        return m.group(1)
    return None


def _subst_bg_placeholders(value, job_id: str | None):
    """Replace `$BG_JOB_ID` with the real id scraped from the previous tool_result; numeric ids
    become int in integer contexts."""
    if isinstance(value, dict):
        return {k: _subst_bg_placeholders(v, job_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst_bg_placeholders(v, job_id) for v in value]
    if not isinstance(value, str) or "$BG_JOB_ID" not in value:
        return value
    if not job_id:
        return value
    if value == "$BG_JOB_ID" and job_id.isdigit():
        return int(job_id)
    return value.replace("$BG_JOB_ID", job_id)


def _resolve_tool_name(req: dict, wanted: str) -> str:
    """Map the tool name a probe wants to call onto the name the build ACTUALLY advertises
    (build-agnostic, no overfitting).

    Argument names are mapped by `_map_args_to_schema`; this maps the tool name itself.
    Sending `write` to a build that advertises `write_file` means the call name never matches, the tool
    never runs, and it looks like "the write issued by the stub did not land" - the harness's call
    shape being wrong, not a missing write tool.

    Rules mirror the argument-name mapping: 1. `wanted` is already in the build's advertised list ->
    use as is; 2. otherwise substitute a name from its synonym group that the build really has;
    3. neither available (or no list at all) -> keep the original name (never invent one).
    """
    names = set()
    for t in req.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    if not names or wanted in names:
        return wanted
    for grp in _TOOL_NAME_SYNONYMS:
        if wanted in grp:
            for cand in grp:
                if cand in names:
                    return cand
            break
    want_cap = canon_tool(wanted)
    if want_cap:
        for cand in sorted(names):
            if canon_tool(cand) == want_cap:
                return cand
    return wanted


def _tool_schema_props(req: dict, tool_name: str) -> set:
    """Argument names a given function tool declares in this request's schema
    (parameters.properties keys). Not found -> empty set."""
    for t in req.get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if not isinstance(fn, dict) or fn.get("name") != tool_name:
            continue
        props = (fn.get("parameters") or {}).get("properties") or {}
        if isinstance(props, dict):
            return set(props.keys())
    return set()


def _map_args_to_schema(req: dict, tool_name: str, pargs: dict) -> dict:
    """Map probe_args parameter names to the schema names the build actually advertises
    (build-agnostic, no overfitting).

    Rules: 1. the name is already in the build schema -> keep it; 2. otherwise substitute a name from
    its synonym group that the build schema really has; 3. none available (or no schema) -> keep the
    original (never invent one). This drives builds that use `path` and builds that use `file_path`
    equally well.
    """
    props = _tool_schema_props(req, tool_name)
    if not props or not isinstance(pargs, dict):
        return dict(pargs or {})
    out = {}
    for k, v in pargs.items():
        target = k
        if k in props:
            target = k
        else:
            for grp in _PARAM_SYNONYMS:
                if k in grp:
                    for cand in grp:
                        if cand in props:
                            target = cand
                            break
                    break
        if target in out:
            raise ValueError(
                f"ambiguous probe arguments: multiple keys map to advertised parameter {target!r}"
            )
        out[target] = v
    return out


def _tool_result_max_bytes(req: dict) -> int:
    """Largest UTF-8 byte size of any SINGLE role=tool message content in this request. Feeds the
    "large tool output is bounded on the wire" assertion: huge results (e.g. 5MB) must be truncated
    or offloaded before being sent to the model, so what goes on the wire should be far smaller than
    the original. Covers OpenAI string content and Anthropic list-of-parts text blocks. No tool
    messages -> 0."""
    mx = 0
    for m in req.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        c = m.get("content")
        if isinstance(c, str):
            mx = max(mx, len(c.encode("utf-8", "replace")))
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    mx = max(mx, len(part["text"].encode("utf-8", "replace")))
    return mx


def _is_empty_content(c) -> bool:
    """Is content an empty / whitespace-only block (a violation)? content=None is legal
    (tool-call-only turn) and does not count. Covers OpenAI string content and Anthropic-style
    list-of-parts empty text parts."""
    if c is None:
        return False
    if isinstance(c, str):
        return c.strip() == ""
    if isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and part.get("type") == "text":
                t = part.get("text")
                if isinstance(t, str) and t.strip() == "":
                    return True
    return False


_VOLATILE_RX = (
    (re.compile("\\d{4}-\\d{2}-\\d{2}([ T]\\d{2}:\\d{2}(:\\d{2})?)?"), "<DATE>"),
    (re.compile("\\b\\d{2}:\\d{2}(:\\d{2})?\\b"), "<TIME>"),
    (re.compile("/\\S*(?:state_smoke|fork_api|d2a)_[A-Za-z0-9_]+"), "<WORKDIR>"),
    (re.compile("\\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\\b"), "<UUID>"),
)


def _mask_volatile(text: str) -> str:
    for rx, rep in _VOLATILE_RX:
        text = rx.sub(rep, text)
    return text


def wire_facts(req: dict, body_text: str) -> dict:
    """Extract wire-compliance facts from the request body (mode-independent, always recorded)."""
    msgs = req.get("messages") or []
    roles = [m.get("role") for m in msgs if isinstance(m, dict)]
    nonleading_system = any((r == "system" for r in roles[1:]))
    seen_ids: set = set()
    provided_ids: set = set()
    orphan = False
    empty_content_block = False
    empty_tool_result = False
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    seen_ids.add(tc["id"])
            if not m.get("tool_calls"):
                if _is_empty_content(m.get("content")):
                    empty_content_block = True
            elif isinstance(m.get("content"), (str, list)) and _is_empty_content(m.get("content")):
                empty_content_block = True
        elif role == "tool":
            tcid = m.get("tool_call_id")
            if tcid is not None:
                provided_ids.add(tcid)
            if tcid is None or tcid not in seen_ids:
                orphan = True
            if _is_empty_content(m.get("content")):
                empty_tool_result = True
        if role in ("user", "system") and _is_empty_content(m.get("content")):
            empty_content_block = True
    dangling_tool_use = any((tcid not in provided_ids for tcid in seen_ids))
    trailing_empty_assistant = False
    if msgs and isinstance(msgs[-1], dict):
        last = msgs[-1]
        if (
            last.get("role") == "assistant"
            and (not last.get("tool_calls"))
            and _is_empty_content(last.get("content"))
        ):
            trailing_empty_assistant = True
    has_reasoning = (
        "reasoning_content" in body_text
        or "thought_signature" in body_text
        or "reasoning_signature" in body_text
        or ('"_reasoning"' in body_text)
    )
    return {
        "msg_roles": roles,
        "nonleading_system": nonleading_system,
        "orphan_tool_result": orphan,
        "lone_surrogate": _has_lone_surrogate(body_text),
        "empty_content_block": empty_content_block,
        "dangling_tool_use": dangling_tool_use,
        "trailing_empty_assistant": trailing_empty_assistant,
        "has_reasoning": has_reasoning,
        "empty_tool_result": empty_tool_result,
    }


def _completion(model: str, *, tool_calls=None, content=None, usage="default", cfg=None) -> dict:
    cfg = cfg if cfg is not None else CONFIG
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    resp = {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "mock",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": msg}],
    }
    if usage == "default":
        pt, ct = cfg.get("usage_tokens", (12, 8))
        resp["usage"] = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}
    elif usage == "null":
        resp["usage"] = None
    elif usage == "missing_field":
        resp["usage"] = {"completion_tokens": 8}
    return resp


def _bash_toolcall(command: str, *, args_json: str | None = None) -> list:
    arguments = args_json if args_json is not None else json.dumps({"command": command})
    return [{"id": "call_mock_1", "type": "function", "function": {"name": "bash", "arguments": arguments}}]


def build_response(
    mode: str, model: str, command: str, cfg=None, nreq: int = 1, req: dict = None
) -> tuple[int, dict]:
    """Return (http_status, body_dict). A None body means a special path (e.g. disconnect).

    nreq = which request this is for the server (1-based), letting "fault then recover" modes switch
    behavior by round.
    """
    if mode == "recover_bad_json":
        if nreq <= 1:
            return (
                200,
                _completion(
                    model,
                    tool_calls=_bash_toolcall(command, args_json='{"command": '),
                    usage="default",
                    cfg=cfg,
                ),
            )
        return (200, _completion(model, tool_calls=_bash_toolcall(command), cfg=cfg))
    if mode == "recover_plain_no_tool":
        if nreq <= 1:
            return (
                200,
                _completion(model, content="Let me think about this first.", usage="default", cfg=cfg),
            )
        return (200, _completion(model, tool_calls=_bash_toolcall(command), cfg=cfg))
    if mode == "normal":
        return (200, _completion(model, tool_calls=_bash_toolcall(command), cfg=cfg))
    if mode == "empty_content_with_tools":
        if nreq <= 2:
            r = _completion(model, tool_calls=_bash_toolcall(command), usage="default", cfg=cfg)
            r["choices"][0]["message"]["content"] = ""
            return (200, r)
        return (200, _completion(model, content="done", usage="default", cfg=cfg))
    if mode == "empty_content_list_with_tools":
        if nreq <= 2:
            r = _completion(model, tool_calls=_bash_toolcall(command), usage="default", cfg=cfg)
            r["choices"][0]["message"]["content"] = [{"type": "text", "text": ""}]
            return (200, r)
        return (200, _completion(model, content="done", usage="default", cfg=cfg))
    if mode == "usage_null":
        return (200, _completion(model, tool_calls=_bash_toolcall(command), usage="null", cfg=cfg))
    if mode == "usage_missing":
        return (200, _completion(model, tool_calls=_bash_toolcall(command), usage="missing_field", cfg=cfg))
    if mode == "usage_omit":
        return (200, _completion(model, tool_calls=_bash_toolcall(command), usage="omit", cfg=cfg))
    if mode == "plain_no_tool":
        content = (cfg.get("reply_content") if cfg else None) or "I have completed the task."
        return (200, _completion(model, content=content, usage="default", cfg=cfg))
    if mode == "bad_json_args":
        return (
            200,
            _completion(
                model, tool_calls=_bash_toolcall(command, args_json='{"command": '), usage="default", cfg=cfg
            ),
        )
    if mode == "newline_in_args":
        return (
            200,
            _completion(
                model,
                tool_calls=_bash_toolcall(command, args_json='{"command": "echo line1\nline2"}'),
                usage="default",
                cfg=cfg,
            ),
        )
    if mode == "unknown_tool":
        tc = [
            {
                "id": "call_x",
                "type": "function",
                "function": {"name": "python", "arguments": json.dumps({"code": "print(1)"})},
            }
        ]
        return (200, _completion(model, tool_calls=tc, usage="default", cfg=cfg))
    if mode == "tool_calls_null":
        resp = _completion(model, content=None, usage="default", cfg=cfg)
        resp["choices"][0]["message"]["tool_calls"] = None
        return (200, resp)
    if mode == "args_missing_command":
        return (
            200,
            _completion(
                model, tool_calls=_bash_toolcall("", args_json='{"foo": "bar"}'), usage="default", cfg=cfg
            ),
        )
    if mode == "parallel_tools":
        tc = [
            {
                "id": "call_a",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo PARA > pa.txt"})},
            },
            {
                "id": "call_b",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo PARB > pb.txt"})},
            },
        ]
        return (200, _completion(model, tool_calls=tc, usage="default", cfg=cfg))
    if mode == "parallel_one_fail":
        tc = [
            {
                "id": "call_ok",
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": "echo OK_SIBLING_4242"})},
            },
            {
                "id": "call_bad",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": "nonexistent_cmd_xyz_9931"}),
                },
            },
        ]
        return (200, _completion(model, tool_calls=tc, usage="default", cfg=cfg))
    if mode == "finish_length":
        resp = _completion(model, content="partial answer that got cut off mid-sen", usage="default", cfg=cfg)
        resp["choices"][0]["finish_reason"] = "length"
        return (200, resp)
    if mode == "multi_choices":
        base = _completion(model, tool_calls=_bash_toolcall(command), cfg=cfg)
        base["choices"].append(
            {
                "index": 1,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "alternative choice text"},
            }
        )
        return (200, base)
    if mode == "empty_command":
        return (
            200,
            _completion(
                model,
                tool_calls=_bash_toolcall("", args_json=json.dumps({"command": ""})),
                usage="default",
                cfg=cfg,
            ),
        )
    if mode == "missing_finish_reason":
        resp = _completion(model, tool_calls=_bash_toolcall(command), cfg=cfg)
        resp["choices"][0].pop("finish_reason", None)
        return (200, resp)
    if mode == "tool_calls_empty":
        resp = _completion(model, content=None, usage="default", cfg=cfg)
        resp["choices"][0]["finish_reason"] = "tool_calls"
        resp["choices"][0]["message"]["tool_calls"] = []
        return (200, resp)
    if mode == "perm_probe":
        if nreq <= 1:
            allow_cmd = (cfg.get("allow_cmd") if cfg else None) or "echo PERM_LIVE_7755 > live.txt"
            deny_cmd = (cfg.get("deny_cmd") if cfg else None) or "touch PERM_DENY_7755.txt"
            probe_tool = (cfg.get("probe_tool") if cfg else None) or "bash"
            if probe_tool == "bash":
                probe_fn = {"name": "bash", "arguments": json.dumps({"command": deny_cmd})}
            else:
                pargs = (cfg.get("probe_args") if cfg else None) or {}
                probe_tool = _resolve_tool_name(req or {}, probe_tool)
                pargs = _map_args_to_schema(req or {}, probe_tool, pargs)
                probe_fn = {"name": probe_tool, "arguments": json.dumps(pargs)}
            tc = [
                {
                    "id": "call_live",
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": allow_cmd})},
                },
                {"id": "call_probe", "type": "function", "function": probe_fn},
            ]
            probe2 = (cfg.get("probe2_cmd") if cfg else None) or ""
            if probe2:
                tc.append(
                    {
                        "id": "call_probe2",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": probe2})},
                    }
                )
            return (200, _completion(model, tool_calls=tc, usage="default", cfg=cfg))
        return (200, _completion(model, content="done", usage="default", cfg=cfg))
    if mode == "int_toolcall_id":
        tc = [
            {
                "id": 12345,
                "type": "function",
                "function": {"name": "bash", "arguments": json.dumps({"command": command})},
            }
        ]
        return (200, _completion(model, tool_calls=tc, usage="default", cfg=cfg))
    if mode == "content_control_token":
        return (
            200,
            _completion(
                model, content="done ]<]minimax[>[ </tool_call> partial result", usage="default", cfg=cfg
            ),
        )
    if mode == "empty_tool_name":
        tc = [
            {
                "id": "call_x",
                "type": "function",
                "function": {"name": "", "arguments": json.dumps({"command": command})},
            }
        ]
        return (200, _completion(model, tool_calls=tc, usage="default", cfg=cfg))
    if mode == "degenerate_finish":
        resp = _completion(model, content=None, usage="default", cfg=cfg)
        resp["choices"][0]["finish_reason"] = "content_filter"
        return (200, resp)
    if mode == "empty_success":
        resp = _completion(model, content="", usage="omit", cfg=cfg)
        resp["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        return (200, resp)
    if mode == "missing_choices":
        return (
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model or "mock",
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )
    if mode == "model_not_found":
        return (
            404,
            {
                "error": {
                    "message": "The model `mock-not-found` does not exist",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )
    if mode == "reasoning_emit":
        resp = _completion(model, tool_calls=_bash_toolcall(command), usage="default", cfg=cfg)
        resp["choices"][0]["message"]["reasoning_content"] = "internal chain-of-thought REASONING_4242"
        return (200, resp)
    if mode == "reasoning_action":
        resp = _completion(model, tool_calls=_bash_toolcall(command), usage="default", cfg=cfg)
        resp["choices"][0]["message"][
            "reasoning_content"
        ] = "Let me think. First I will run:\n```bash\ntouch REASON_PWNED_7731.txt\n```\nbut actually I'll just do the benign step."
        return (200, resp)
    if mode == "content_action":
        resp = _completion(model, tool_calls=_bash_toolcall(command), usage="default", cfg=cfg)
        resp["choices"][0]["message"][
            "content"
        ] = "Here is what I would run:\n\n```bash\ntouch CONTENT_PWNED_7731.txt\n```\n\nOr from a shell prompt:\n\n$ touch CONTENT_PWNED2_7731.txt\n\nI have already made the change with the tool call above."
        return (200, resp)
    if mode == "errors_then_ok":
        nb = (cfg.get("err_before") if cfg else None) or 2
        es = (cfg.get("err_status") if cfg else None) or 429
        if nreq <= nb:
            et = "rate_limit_error" if es == 429 else "server_error"
            return (es, {"error": {"message": f"mock injected {es}", "type": et}})
        return (200, _completion(model, tool_calls=_bash_toolcall(command), usage="default", cfg=cfg))
    if mode == "seq_probe":
        rounds = (cfg.get("rounds") if cfg else None) or []
        base = int((cfg.get("rounds_base") if cfg else 0) or 0)
        nround = nreq - base
        if 1 <= nround <= len(rounds):
            specs = rounds[nround - 1] or []
            if any((isinstance(sp, dict) and sp.get("name") == "__finish__" for sp in specs)):
                return (200, _completion(model, content="phase-done", usage="default", cfg=cfg))
            job_id = _extract_bg_job_id(_latest_tool_batch(req or {}))
            if job_id:
                cfg["bg_job_id_cached"] = job_id
            else:
                job_id = cfg.get("bg_job_id_cached") if cfg else None
            tcs = []
            for i, sp in enumerate(specs):
                nm = sp.get("name", "bash")
                if isinstance(nm, (list, tuple)):
                    choices = [str(x) for x in nm if x]
                    advertised = set(_tool_names(req or {}))
                    nm = next((x for x in choices if x in advertised), choices[0] if choices else "bash")
                logical, nm = (nm, _resolve_tool_name(req or {}, nm))
                if logical == "bash":
                    pargs = {"command": sp.get("command", "")}
                    for k, v in sp.items():
                        if k not in ("name", "command", "args"):
                            pargs[k] = v
                    pargs.update(sp.get("args") or {})
                    pargs = _subst_bg_placeholders(pargs, job_id)
                    arguments = json.dumps(_map_args_to_schema(req or {}, nm, pargs))
                else:
                    pargs = _subst_bg_placeholders(dict(sp.get("args") or {}), job_id)
                    pargs = _map_args_to_schema(req or {}, nm, pargs)
                    arguments = json.dumps(pargs)
                tcs.append(
                    {
                        "id": f"call_{nreq}_{i}",
                        "type": "function",
                        "function": {"name": nm, "arguments": arguments},
                    }
                )
            if not tcs:
                tcs = _bash_toolcall("true")
            return (200, _completion(model, tool_calls=tcs, usage="default", cfg=cfg))
        return (200, _completion(model, content="done", usage="default", cfg=cfg))
    if mode == "fixed_reply":
        content = (cfg.get("reply_content") if cfg else None) or "PUREANS_7731"
        return (200, _completion(model, content=content, usage="default", cfg=cfg))
    if mode == "http_5xx_then_ok":
        if nreq <= 1:
            return (503, {"error": {"message": "upstream temporarily unavailable", "type": "server_error"}})
        return (200, _completion(model, tool_calls=_bash_toolcall(command), cfg=cfg))
    if mode == "http_400":
        return (400, {"error": {"message": "mock injected 400", "type": "invalid_request_error"}})
    return (200, _completion(model, tool_calls=_bash_toolcall(command), cfg=cfg))


def non_json_decode_fixture():
    """HTTP 200 text/plain isolates body decoding from HTTP status handling."""
    return (200, b"API response is not JSON", "text/plain")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_raw(self, status: int, data: bytes, ctype: str):
        """Send a non-JSON or custom body (tests client robustness against bad response bodies)."""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _send_sse(self, model: str, content: str):
        """OpenAI-compatible SSE streaming response (tui_smoke e2e stream=True depends on it; answering a
        streaming request with plain JSON would cause false negatives)."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        cid = "chatcmpl-mock"
        created = int(time.time())
        chunks = [
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            },
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"content": content or ""}, "finish_reason": None}],
            },
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        for ch in chunks:
            self.wfile.write(f"data: {json.dumps(ch, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")

    def do_GET(self):
        cfg = getattr(self.server, "cfg", CONFIG)
        if self.path.startswith("/d2a-network-probe-9931"):
            cfg.setdefault("web_gets", []).append({"path": self.path, "at": time.time()})
            self._send(200, {"probe": "NETWORK_ON_OK_9931"})
            return
        if self.path.startswith("/__d2a_seen"):
            from urllib.parse import urlparse, parse_qs

            token = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            seen = bool(token) and any(
                (
                    (rec.get("probes") or {}).get(token)
                    or (rec.get("has_probe") and token == str(cfg.get("probe_token") or ""))
                    for rec in cfg.get("requests") or []
                )
            )
            self._send(200, {"seen": seen, "token": token, "requests": len(cfg.get("requests") or [])})
            return
        self._send(200, {"object": "list", "data": []})

    def do_POST(self):
        cfg = getattr(self.server, "cfg", CONFIG)
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b""
        has_null = b"\x00" in raw
        has_escaped_null = b"\\u0000" in raw or b"\\u0000".upper() in raw
        any_null = has_null or has_escaped_null
        body_text = raw.decode("utf-8", errors="replace")
        try:
            req = json.loads(body_text)
        except Exception:
            req = {}
        model = req.get("model", "mock")
        rec = {
            "path": self.path,
            "n_messages": len(req.get("messages", []) or []),
            "model": model,
            "has_null_byte": has_null,
            "has_escaped_null": has_escaped_null,
            "has_any_null": any_null,
            "body_bytes": len(raw),
            "tool_result_max_bytes": _tool_result_max_bytes(req),
            "authorization": self.headers.get("Authorization", ""),
            "has_tools": bool(req.get("tools")),
            "stream": bool(req.get("stream")),
            "tool_names": _tool_names(req),
            "tool_result_texts": [t[:800] for t in _latest_tool_batch(req)],
            "bg_job_id": _extract_bg_job_id(_latest_tool_batch(req)),
            "msg_role_tail": [
                m.get("role") if isinstance(m, dict) else type(m).__name__
                for m in (req.get("messages") or [])[-6:]
            ],
        }
        rec.update(wire_facts(req, body_text))
        if cfg.get("target_command"):
            rec["target_exchange"] = _target_exchange_facts(
                req,
                str(cfg["target_command"]),
                str(cfg.get("target_left") or ""),
                str(cfg.get("target_right") or ""),
            )
        if cfg.get("structured_prompt_token"):
            rec["structured_prompt"] = _structured_prompt_facts(
                req,
                str(cfg["structured_prompt_token"]),
                str(cfg.get("structured_prompt_end") or ""),
                str(cfg.get("structured_prompt_expected") or ""),
                str(cfg.get("structured_prompt_fragment") or ""),
            )
        pt = cfg.get("probe_token") or ""
        rec["has_probe"] = bool(pt) and pt in body_text
        rec["has_probe_in_system"] = bool(pt) and any(
            (
                pt in json.dumps(m.get("content"), ensure_ascii=False)
                for m in req.get("messages") or []
                if isinstance(m, dict) and m.get("role") == "system"
            )
        )
        rec["probes"] = {tok: tok in body_text for tok in cfg.get("probe_tokens") or []}
        _sys_blob = "\n".join(
            (
                json.dumps(m.get("content"), ensure_ascii=False)
                for m in req.get("messages") or []
                if isinstance(m, dict) and m.get("role") == "system"
            )
        )
        rec["probes_in_system"] = {tok: tok in _sys_blob for tok in cfg.get("probe_tokens") or []}
        rec["system_sha"] = hashlib.sha256(_mask_volatile(_sys_blob).encode("utf-8", "replace")).hexdigest()[
            :16
        ]
        cfg["requests"].append(rec)
        mode = cfg["mode"]
        if cfg.get("strict") and (
            rec["nonleading_system"] or rec["orphan_tool_result"] or rec["lone_surrogate"] or any_null
        ):
            self._send(400, {"error": {"message": "strict wire violation", "type": "invalid_request_error"}})
            return
        if mode == "empty_body":
            self._send_raw(200, b"", "application/json")
            return
        if mode == "non_json_body":
            status, body, content_type = non_json_decode_fixture()
            self._send_raw(status, body, content_type)
            return
        if mode == "http_401":
            self._send(401, {"error": {"message": "invalid api key", "type": "authentication_error"}})
            return
        if mode == "http_500":
            self._send(500, {"error": {"message": "internal server error", "type": "server_error"}})
            return
        if mode == "stall":
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "9999")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"id":"chatcmpl-mock","choices":[{"index":0,"message":{"role":"assi')
                self.wfile.flush()
                time.sleep(60)
            except Exception:
                pass
            self.close_connection = True
            return
        if mode == "disconnect":
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "9999")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b'{"id":"chatcmpl-mock","choices":[{"index":0,"message":{"role":"assi')
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            return
        if mode == "reject_nullbyte" and any_null:
            self._send(
                400, {"error": {"message": "string contains null byte", "type": "invalid_request_error"}}
            )
            return
        if cfg.get("reject_substr") and cfg["reject_substr"].encode() in raw:
            self._send(
                400,
                {
                    "error": {
                        "message": f"unknown parameter {cfg['reject_substr']}",
                        "type": "invalid_request_error",
                    }
                },
            )
            return
        status, body = build_response(
            mode if mode != "reject_nullbyte" else "normal",
            model,
            cfg["command"],
            cfg=cfg,
            nreq=len(cfg["requests"]),
            req=req,
        )
        if req.get("stream") and status == 200 and isinstance(body, dict):
            msg = (body.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content")
            if content is not None:
                self._send_sse(model, content)
                return
        self._send(status, body)


def make_server(cfg: dict, port: int = 0) -> ThreadingHTTPServer:
    """Create a mock server with its own cfg (port=0 picks a free port). One per case when running concurrently."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.cfg = cfg
    return srv


def run(port: int):
    srv = make_server(CONFIG, port)
    print(f"[mock_gateway] listening on 127.0.0.1:{port} mode={CONFIG['mode']}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9009)
    ap.add_argument("--mode", default=None)
    args = ap.parse_args()
    if args.mode:
        CONFIG["mode"] = args.mode
    run(args.port)
