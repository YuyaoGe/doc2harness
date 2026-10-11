#!/usr/bin/env python3
"""Deterministic smoke for file-tool integrity (mock driven, zero real-model budget).

Covers glob (sorting and truncation), grep (.gitignore, type filter, default output mode, head_limit),
notebook editing (cell isolation), `/rewind` restoring an edited file, symlink and out-of-workspace reads,
`memory_get` path fencing, extension definitions reached through symlinks, and bounded character-device reads.

Mechanism: reuses perm_smoke's subprocess bootstrap (_child_code/_run_child/_BOOT) plus mock_gateway's `seq_probe`
multi-round tool sequencer. Each round issues, in parallel, a liveness bash (writing live.txt as execution evidence)
and the tool under test. Observation: file contents after the change through res['tgt'], tool results fed back
into messages through res['mtok'].

No false positives: (1) every "must be blocked/untouched" case has a "must land" control proving the tool really can
write/read and the probe works, with suite-level gating -- when the control is not PASS its dependants become
inconclusive, so "the tool never worked, hence the file did not change" cannot masquerade as a correct refusal;
(2) an N/A guard: if the build does not advertise the tool under test the row is inconclusive (reusing
perm_smoke's `_adv`).

Usage: python fileio_smoke.py <agent_dir> [--emit-json]   (needs an interpreter that has litellm)
"""
from __future__ import annotations

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
import mock_gateway as MG  # noqa: E402
import perm_smoke as PS  # noqa: E402
import smoke_contract as SC  # noqa: E402

TGT = PS.TGT          # "tgt.txt"
TGT2 = PS.TGT2        # "tgt2.txt"
ORIG = PS.ORIG        # "ORIG_PERM_0001\n"
_ALLOW = PS._ALLOW    # echo PERM_LIVE_7755 > live.txt


# ---- N/A guard + executed evidence ----
def _na(res, reqs, err, tool):
    if res is None:
        return (False, f"the child process reported no result (crash/timeout) reqs={reqs} err={err[-160:]!r}")
    adv = res.get("_adv") or []
    tools = list(tool) if isinstance(tool, (list, tuple, set)) else [tool]
    # Match by normalised capability family rather than exact name (see mock_gateway.canon_tool).
    if not MG.adv_has_tool(adv, tools):
        return (True, f"inconclusive: the build does not advertise the standalone tool {tools!r} to the model (adv={adv})")
    return None


def _alive(res, reqs):
    return (res.get("live") is True) and (reqs >= 2)


def _target_result(res):
    """Return only the declared target call's latest tool result."""
    value = res.get("_target_tool_result")
    return "" if value is None else str(value)


def chk_transition_control(expected, tool):
    """Require every named file-state boundary of a control to be witnessed."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        actual = {key: res.get(key) for key in expected}
        ok = actual == expected and _alive(res, reqs)
        return ok, (
            f"transition expected={expected!r} actual={actual!r} "
            f"live={res.get('live')} reqs={reqs}"
        )
    return _f


def chk_memory_get_event(marker, tool="memory_get"):
    """Require the marker to be newly observed after the memory_get phase."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        before = res.get("phase1_mtok") or ""
        after = res.get("phase2_mtok") or ""
        delta = after.count(marker) - before.count(marker)
        ok = delta > 0 and _alive(res, reqs)
        return ok, (
            f"event-scoped marker delta={delta} before_count={before.count(marker)} "
            f"after_count={after.count(marker)} live={res.get('live')} reqs={reqs}"
        )
    return _f


def chk_fixture_markers(markers, tool="bash"):
    """Require all independently probed filesystem-shape markers."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        mtok = res.get("mtok") or ""
        missing = [marker for marker in markers if marker not in mtok]
        ok = not missing and _alive(res, reqs)
        return ok, (
            f"fixture markers missing={missing} live={res.get('live')} reqs={reqs}"
        )
    return _f


_REWIND_UNKNOWN_SIGS = (
    "unknown command", "unknown slash", "unrecognized", "not recognized",
    "no such command", "is not a command", "not a valid command", "not implemented",
    "未知命令", "无此命令", "不支持的命令",
)


def chk_rewind_restores(expected, tool):
    """v6§5.1:262: `/rewind` (`/checkpoint`) can RESTORE CODE to an earlier point.

    Printing a checkpoint menu is not enough; the spec requires a real restore. This row judges the effect:
    after the write tool changed a file, `/rewind` must return the target file to its exact pre-change content.

    Three states:
    - `/rewind` not recognised => NA (a missing command is not this row's concern; no double penalty)
    - the `undo_reverts_overwrite_control` control failing => NA (edit tracking was never established)
    - restored to the pre-change content => PASS; only a menu, or only truncating the conversation while the file is
      untouched => FAIL
    """
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        rets = res.get("mid1_rets") or []
        blob = " ".join(str(x) for x in rets).lower()
        if blob and any(sig in blob for sig in _REWIND_UNKNOWN_SIGS):
            return True, f"inconclusive: `/rewind` is not recognised out={blob[:110]!r}"
        before, after = res.get("phase1_tgt"), res.get("after_mid1_tgt")
        if before == expected:
            return True, (f"inconclusive: the change itself never landed (still the original after phase1), and with "
                          f"nothing changed there is nothing to restore phase1_tgt={before!r}")
        ok = (after == expected)
        return ok, (f"restored to the pre-change content after /rewind={ok} (after the change={before!r} -> after "
                    f"rewind={after!r}, expected={expected!r}) out={blob[:90]!r}"
                    f" (v6§5.1:262 /rewind must really restore code; only a menu, or only touching the conversation, = FAIL)")
    return _f


def chk_mtok_contains(token, tool="read"):
    """Read/search control: the tool result must contain token (fed back into messages), proving the read path works."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        seen = token in (res.get("mtok") or "")
        ok = seen and _alive(res, reqs)
        return ok, f"result contains {token!r}={seen} live={res.get('live')} reqs={reqs} (read results should be fed back into messages)"
    return _f


def chk_notebook_isolated(tool="notebook_edit"):
    """§1.1 notebook-edit: replace must change the target cell, leave the neighbouring cells' WHOLE OBJECTS untouched
    and remove the old value. The notebook JSON on disk is parsed directly, so checking only a source marker cannot
    miss a neighbouring cell's metadata/outputs/execution_count being rebuilt."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        raw = res.get("nb") or ""
        try:
            nb = json.loads(raw)
            cells = nb.get("cells") if isinstance(nb, dict) else None
        except Exception:
            cells = None
        shape_ok = isinstance(cells, list) and len(cells) == 3
        blob = json.dumps(cells, ensure_ascii=False) if shape_ok else ""
        new_in = "CELL1_NEW" in blob
        old_gone = "CELL1_OLD" not in blob
        k0 = shape_ok and cells[0] == _NB_CELL0
        k2 = shape_ok and cells[2] == _NB_CELL2
        ok = shape_ok and new_in and old_gone and k0 and k2 and _alive(res, reqs)
        return ok, (f"notebook parses with 3 cells={shape_ok} target cell changed={new_in} old value gone={old_gone} "
                    f"neighbour cell0 intact={k0} neighbour cell2 intact={k2} live={res.get('live')} reqs={reqs}"
                    f" (notebook_edit replace must change only the target cell and leave the other cells' whole "
                    f"objects untouched)")
    return _f


def chk_grep_paths_only(paths, body_tokens, tool="grep"):
    """grep `files_with_matches`: it must return the matching file paths and must not carry matched line bodies.

    Only the unique file path is wanted.
    The body tokens exist only in the file bodies on disk, never in tool arguments, so their presence in mtok proves
    the implementation degraded into content mode.
    """
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        result = _target_result(res)
        has_paths = all(path in result for path in paths)
        leaked = [token for token in body_tokens if token in result]
        count_records = re.findall(r"(?:^|\n)\s*[^:\n]+:\s*\d+\s*(?:\n|$)", result)
        ok = has_paths and not leaked and not count_records and _alive(res, reqs)
        return ok, (
            f"target result has every path={has_paths} body leaked={leaked} count records={count_records} "
            f"live={res.get('live')} reqs={reqs}"
            " (v6§1.1:140 files_with_matches returns matching files only and must not degrade into content)"
        )
    return _f


def chk_grep_type_filter(inc_file, exc_file, tool="grep"):
    """v6§1.1:140 grep ("can be narrowed by glob or TYPE"): grep with type:py must match only the .py and exclude the
    .txt. The seed puts the same needle in inc(.py) and exc(.txt); with type in effect mtok contains inc and not exc
    = PASS, while ignoring type (no type in the schema, or a handler that never reads it) matches both and mtok
    contains exc = FAIL. No false positives: the inc hit proves grep really ran and type did not wrongly exclude the
    py; only "exc present = type had no effect" fails.
    A grep whose schema has no `type` parameter, or whose handler never reads it, searches every type."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        mtok = res.get("mtok") or ""
        has_inc = inc_file in mtok
        has_exc = exc_file in mtok
        ok = has_inc and (not has_exc) and _alive(res, reqs)
        return ok, (f"contains {inc_file} (py hit)={has_inc} contains {exc_file} (type should exclude it)={has_exc} "
                    f"live={res.get('live')} reqs={reqs}"
                    f" (v6§1.1:140 grep type:py must match only the .py and exclude the .txt; the .txt appearing "
                    f"means the type filter was silently ignored)")
    return _f


def chk_grep_finds(needle_file, tool="grep"):
    """Control: grep finds the needle in a non-ignored file, proving the grep tool path works and the probe is not running vacuously."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        mtok = res.get("mtok") or ""
        ok = (needle_file in mtok) and _alive(res, reqs)
        return ok, (f"hit {needle_file}={needle_file in mtok} live={res.get('live')} reqs={reqs}"
                    f" (grep control: the needle must be found in an ordinary file)")
    return _f


def chk_extension_inside_symlink_control(res, reqs, err):
    """Inward-symlink positive control for extension definitions.

    Truth table:
      * harness could not prove every definition node is a symlink resolving inside repo -> NA;
      * child did not execute the real model/tool path -> FAIL;
      * at least one of commands/skills/hooks/plugins emits its definition-only external marker
        -> PASS; none emits a marker -> FAIL.

    Per-face applicability is retained in `_extension_seen`; the paired hard target repeats this
    inward run and only judges faces whose inward marker was actually observed.
    """
    if res is None:
        return False, f"no result from the child process reqs={reqs} err={(err or '')[-160:]!r}"
    if res.get("_extension_fixture_ok") is not True:
        return True, (
            f"inconclusive: the inward extension symlink fixture did not self-prove "
            f"error={res.get('_extension_fixture_error')!r}"
        )
    seen = {
        face: bool((res.get("_extension_seen") or {}).get(face))
        for face in _EXTENSION_FACES
    }
    live = res.get("live") is True and int(res.get("_extension_request_count") or 0) >= 2
    applicable = [face for face, value in seen.items() if value]
    ok = live and bool(applicable)
    return ok, (
        f"inward definition exact markers={seen} applicable={applicable} "
        f"live={res.get('live')} requests={res.get('_extension_request_count')} reqs={reqs}"
        " (v6§7:433-436: legal in-workspace definitions must still be discoverable and executable; the fence must "
        "not be passed by refusing every symlink)"
    )


def chk_extension_symlink_fence(res, reqs, err):
    """Paired checker: extension definitions must not be reached through an outward symlink.

    Truth table:
      * either filesystem fixture cannot prove the inward/outward realpath relation -> NA;
      * paired inward run is not live or observes no definition-only marker -> NA;
      * paired outward run is not live -> NA (absence would be vacuous);
      * any outward command/skill/hook/plugin marker is observed -> FAIL;
      * zero outward markers for every face, with at least one inward-applicable face -> PASS.

    Marker provenance is external to the product workspace: command/hook/plugin execution appends
    exact lines to harness-owned files, while command/skill/plugin discovery uses full high-entropy
    tokens that occur only in the symlink target definition.  No definition name, error substring,
    or cumulative `mtok` approximation is accepted.
    """
    if res is None:
        return False, f"no result from the paired extension child process reqs={reqs} err={(err or '')[-160:]!r}"
    if res.get("_extension_inside_fixture_ok") is not True \
            or res.get("_extension_outside_fixture_ok") is not True:
        return True, (
            "inconclusive: the paired extension symlink fixture did not self-prove "
            f"inside_error={res.get('_extension_inside_fixture_error')!r} "
            f"outside_error={res.get('_extension_outside_fixture_error')!r}"
        )
    inside_live = res.get("_extension_inside_live") is True \
        and int(res.get("_extension_inside_request_count") or 0) >= 2
    outside_live = res.get("_extension_outside_live") is True \
        and int(res.get("_extension_outside_request_count") or 0) >= 2
    inside = {
        face: bool((res.get("_extension_inside_seen") or {}).get(face))
        for face in _EXTENSION_FACES
    }
    outside = {
        face: bool((res.get("_extension_outside_seen") or {}).get(face))
        for face in _EXTENSION_FACES
    }
    applicable = [face for face, value in inside.items() if value]
    if not inside_live or not applicable:
        return True, (
            f"inconclusive: the paired inward control did not hold live={inside_live} markers={inside}"
        )
    if not outside_live:
        return True, (
            f"inconclusive: the paired outward run was not live requests="
            f"{res.get('_extension_outside_request_count')}, so zero markers cannot serve as proof of refusal"
        )
    escaped = [face for face, value in outside.items() if value]
    ok = not escaped
    return ok, (
        f"inward exact markers={inside} applicable={applicable}; "
        f"outward exact markers={outside} escaped={escaped} reqs={reqs}"
        " (v6§7:433-436 x v6§0.1.1 --repo-dir workspace boundary: the commands/skills/hooks/plugins definition "
        "resolver must realpath-contain; an outward symlink must not be discovered or executed)"
    )


_ALLOW_SPEC = {"name": "bash", "command": _ALLOW}

# Extension definitions reached through symlinks.  Wire markers occur only in the
# definition bodies; execution markers are exact lines in harness-owned files
# outside the product workspace.
_EXTENSION_FACES = ("commands", "skills", "hooks", "plugins")
_EXTENSION_WIRE_MARKERS = {
    "commands": "EXTSYM_COMMAND_BODY_9417",
    "skills": "EXTSYM_SKILL_BODY_9418",
    "plugins": "EXTSYM_PLUGIN_BODY_9419",
}
_EXTENSION_EXEC_MARKERS = {
    "commands": "EXTSYM_COMMAND_EXEC_9420",
    "hooks": "EXTSYM_HOOK_EXEC_9421",
    "plugins": "EXTSYM_PLUGIN_EXEC_9422",
}
_EXTENSION_SETUP = (
    "/mode yolo",
    "/extsymcommand",
    "/skill extsymskill",
    "/extsymplugin",
)

# grep/.gitignore fixtures: git init (rg honours .gitignore for untracked files inside a git repo) plus a
# live.txt write as execution evidence
_GI_NEEDLE = "NEEDLE_GI_7731"
# notebook_edit fixture: neighbouring cells carry distinct metadata/outputs/execution_count so isolation can be
# asserted structurally.
_NB_CELL0 = {"cell_type": "markdown", "metadata": {"tags": ["KEEP0"], "nested": {"k": 0}},
             "source": ["CELL0_KEEP\n"]}
_NB_CELL2 = {"cell_type": "code", "metadata": {"tags": ["KEEP2"], "nested": {"k": 2}},
             "source": ["CELL2_KEEP\n"], "outputs": [{"output_type": "stream", "name": "stdout",
                                                       "text": ["KEEP2_OUTPUT\n"]}],
             "execution_count": 42}
_NB_JSON = json.dumps({"cells": [
    _NB_CELL0,
    {"cell_type": "code", "metadata": {"target": True}, "source": ["CELL1_OLD\n"],
     "outputs": [], "execution_count": None},
    _NB_CELL2],
    "metadata": {}, "nbformat": 4, "nbformat_minor": 5})


def _echo_assembled(prefix: str, rest: str) -> dict:
    """Print a sentinel whose full string never appears in the command text.

    Why it must be assembled: `mtok` holds both tool results AND the command echo, so a token appearing verbatim in
    the command makes the assertion trivially true -- an implementation that never executed would PASS.
    """
    return {"name": "bash", "command": f"python3 -c \"print('{prefix}' + '{rest}')\""}


def chk_mtok_has(marker, tool, why=""):
    """marker must appear in mtok (printed by _byte_probe, standing for some byte-level fact holding)."""
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        mtok = res.get("mtok") or ""
        ok = (marker in mtok) and _alive(res, reqs)
        return ok, (f"mtok contains {marker!r}? {marker in mtok} live={res.get('live')} reqs={reqs}"
                    f"{' (' + why + ')' if why else ''}")
    return _f


def chk_mtok_lacks(marker, probe, tool, why=""):
    """marker must NOT appear in mtok, but probe must (proving the round really ran rather than passing vacuously).

    `mtok` is `_mblob()[-12000:]`, i.e. the TAIL only, so probe must be something that lands in the tail (usually a
    final `echo XXX_DONE_7755` round). A marker at the HEAD of a huge output would fall out of the observation window
    even for a perfectly correct implementation -- a false negative the harness itself would introduce.

    Without the probe half, a build whose tool never executed would falsely PASS simply because the marker is absent.
    """
    def _f(res, reqs, err):
        ng = _na(res, reqs, err, tool)
        if ng is not None:
            return ng
        mtok = res.get("mtok") or ""
        ran = probe in mtok
        bad = marker in mtok
        ok = ran and (not bad) and _alive(res, reqs)
        return ok, (f"really ran({probe})={ran} forbidden {marker!r} present={bad} "
                    f"live={res.get('live')} reqs={reqs}{' (' + why + ')' if why else ''}")
    return _f


CASES = [

    # ===== glob: the tool path itself must work =====
    # v6§1.1:141 does not define whether `**` matches zero directory levels, and an fnmatch-based glob reads
    # `**/*.py` as requiring a `/`. This control only asks whether glob works at all, so the same round also
    # sends the equivalent pattern without `**`; either hit proves the path works. Only the control is relaxed,
    # never any assertion it gates.
    dict(id="glob_finds_inside_control", kind="control", tool="glob", setup=["/mode yolo"],
         seed={"inside_ok.py": "x\n"},
         rounds=[[_ALLOW_SPEC, {"name": "glob", "args": {"pattern": "**/*.py"}},
                  {"name": "glob", "args": {"pattern": "*.py"}}]],
         check=chk_mtok_contains("inside_ok", "glob")),

    # ===== grep control (§1.1 grep) =====
    dict(id="grep_finds_needle_control", kind="control", tool="grep", setup=["/mode yolo"],
         seed={"plain_gi.txt": f"{_GI_NEEDLE} here\n"},
         rounds=[[_ALLOW_SPEC, {"name": "grep", "args": {"pattern": _GI_NEEDLE, "path": "."}}]],
         check=chk_grep_finds("plain_gi.txt", "grep")),

    # Extension symlink fence: zero-weight inward symlink control.
    # The case-local harness creates symlinks for the four definition files (commands/skills/hooks/plugins) before
    # the Session is constructed, with every realpath inside the workspace. At least one definition surface must
    # prove discovery/execution through a definition-only wire token or a harness-owned exact execution line, so the
    # target applies surface by surface.
    dict(id="extension_inside_symlink_discovery_control", kind="reject_soft", tool="bash",
         setup=list(_EXTENSION_SETUP), seed={}, rounds=[[_ALLOW_SPEC]],
         extension_harness="inside",
         check=chk_extension_inside_symlink_control, blocking=False),
    # target: the same case first runs the inward counterfactual, then moves only the four definition symlinks'
    # realpaths outside the workspace; command name, skill name, hook event, plugin manifest and mock rounds are
    # otherwise identical.
    # Anchor v6§7:433-436 x v6§0.1.1: the extension resolver is a workspace-convention discovery surface and must not
    # read or execute external definitions through a symlink; any strict external marker means FAIL.
    dict(id="extension_definition_symlink_outside_fenced", kind="reject",
         gate="extension_inside_symlink_discovery_control", tool="bash",
         setup=list(_EXTENSION_SETUP), seed={}, rounds=[[_ALLOW_SPEC]],
         extension_harness="paired",
         check=chk_extension_symlink_fence),
    # ===== v6§1.1:140: grep's `type` file-type filter must take effect =====
    # A grep schema/handler without a `type` parameter drops a supplied type and
    # every type is searched; others do `if type: cmd += ["-t", type]` and narrow correctly. The seed puts the same
    # needle in a .py and a .txt: with type:py in effect, only the .py matches.
    # Gated on the grep control. No false positives: the included .py matching proves grep really ran, and the
    # excluded .txt matching means type had no effect => FAIL.
    dict(id="grep_type_filter_ignored", kind="reject", gate="grep_finds_needle_control",
         tool="grep", setup=["/mode yolo"],
         seed={"gt_hit.py": "NEEDLE_GTYPE_7731 python\n", "gt_hit.txt": "NEEDLE_GTYPE_7731 text\n"},
         rounds=[[_ALLOW_SPEC, {"name": "grep", "args": {"pattern": "NEEDLE_GTYPE_7731", "path": ".", "type": "py"}}]],
         check=chk_grep_type_filter("gt_hit.py", "gt_hit.txt", "grep")),

    dict(id="undo_reverts_overwrite_control", kind="control", tool="write", setup=["/mode yolo"],
         seed={TGT: ORIG},
         phase1_rounds=[[_ALLOW_SPEC, {"name": "read", "args": {"file_path": TGT}}],
                        [{"name": "write", "args": {"file_path": TGT, "content": "OVR_BODY_7755\n"}}]],
         mid_slash=["/undo"],
         phase2_rounds=[[{"name": "bash", "command": "echo POSTUNDO_7755"}]],
         check=chk_transition_control({
             "phase1_tgt": "OVR_BODY_7755\n",
             "after_mid1_tgt": ORIG,
             "tgt": ORIG,
         }, "write")),
    # v6§5.1:262 /rewind must really restore, not just print a checkpoint menu.
    # The trigger shape differs between implementations, and the SAME `/rewind N` can mean opposite things:
    #   - N = how many steps back (calls undo N times) => `/rewind 1` works, `/rewind 0` is a no-op;
    #   - N = a checkpoint index (only [0] existing) => `/rewind 1` is out of range;
    #   - a bare `/rewind` restores (popping the undo stack directly).
    # => all three are sent. Sending extra cannot over-roll-back: there is only one edit, so once restored the rest
    #   are no-ops or menu listings, and the assertion only looks at whether the final state equals the pre-edit one.
    #   Gated on the undo control: if edit tracking was never established the row is NA rather than unfair.
    # A checkpoint taken only at the start of a turn, covering only files already read, misses a file read and
    # changed within the same turn; a user editing a file in their first turn could not restore it either, so that
    # is scored FAIL as an implementation shortfall, not treated as a fixture problem.
    dict(id="rewind_restores_edited_file", kind="reject", gate="undo_reverts_overwrite_control",
         tool="write", setup=["/mode yolo"], seed={TGT: ORIG},
         phase1_rounds=[[_ALLOW_SPEC, {"name": "read", "args": {"file_path": TGT}}],
                        [{"name": "write", "args": {"file_path": TGT, "content": "RWOVR_8821\n"}}]],
         mid_slash=["/rewind 1", "/rewind 0", "/rewind"],
         phase2_rounds=[[{"name": "bash", "command": "echo POSTRW_8821"}]],
         check=chk_rewind_restores(ORIG, "write")),
    # ===== notebook_edit cell isolation (§1.1 notebook-edit): replace the target cell, leave the others alone =====
    # Notebook tools differ in argument names (e.g. notebook_path/cell_number/new_source/edit_mode), so canonical
    #   arguments go through the shared schema mapper; a read round comes first so a read-before-edit gate does
    #   not refuse the edit.
    dict(id="notebook_edit_cell_isolation", kind="reject", tool="notebook_edit", setup=["/mode yolo"],
         seed={"nb.ipynb": _NB_JSON},
         rounds=[[_ALLOW_SPEC, {"name": "read", "args": {"file_path": "nb.ipynb"}}],
                 [{"name": ["notebook_edit", "notebook-edit"],
                   "args": {"file_path": "nb.ipynb", "cell_index": 1,
                            "mode": "replace", "source": "CELL1_NEW"}}],
                 [{"name": "bash", "command": "cat nb.ipynb"}]],
         check=chk_notebook_isolated(("notebook_edit", "notebook-edit"))),
    dict(id="glob_truncation_keeps_sorted_end", kind="reject", gate="glob_finds_inside_control",
         tool="glob", setup=["/mode yolo"], seed={},
         rounds=[[_ALLOW_SPEC,
                  {"name": "bash",
                   "command": "mkdir -p gord && python3 -c \"import pathlib;"
                              "pathlib.Path('gord/OLD' + 'EST_7755.ordx').write_text('x')\" && "
                              "touch -t 200001010000 gord/OLD*_7755.ordx && "
                              "python3 -c \"import os\nfor i in range(800): "
                              "open('gord/n%d.ordx' % i,'w').write('x')\" && echo ORD_SEEDED_7755"}],
                 [{"name": "glob", "args": {"pattern": "**/*.ordx"}}]],
         check=chk_mtok_lacks("OLDEST_7755", ".ordx", "glob",
                              "derived: §141 requires mtime ordering plus returning only what fits the cap => what gets cut "
                              "must be the tail of that order; keeping the oldest file while dropping newer ones violates both"),
         blocking=False),
    dict(id="notebook_edit_delete_keeps_neighbors", kind="reject", gate="notebook_edit_cell_isolation",
         tool="notebook_edit", setup=["/mode yolo"], seed={"nb.ipynb": _NB_JSON},
         rounds=[[_ALLOW_SPEC, {"name": "read", "args": {"file_path": "nb.ipynb"}}],
                 [{"name": ["notebook_edit", "notebook-edit"],
                   "args": {"file_path": "nb.ipynb", "cell_index": 1,
                            "mode": "delete"}}],
                 # The assertion is structural (cell count) rather than "CELL1_OLD must not appear": the `read`
                 #   round added to satisfy the read-before-edit gate pushes the whole notebook (including
                 #   CELL1_OLD) into mtok, and a "lacks" assertion scans exactly mtok => never satisfiable (that
                 #   fixture, not the product, would decide the verdict).
                 #   General rule: a "lacks" assertion is poisoned by any round that echoes file content; assert
                 #   structure or a sentinel-ised derived value instead.
                 #   The cell count is assembled in python so the full string never appears in the command text
                 #   (command echo can reach mtok).
                 [{"name": "bash",
                   "command": "python3 -c \"import json;print('NBCELLS' + '_' + "
                              "str(len(json.load(open('nb.ipynb'))['cells'])))\" && echo NBDEL_DONE_7755"}]],
         check=chk_mtok_has("NBCELLS_2", "notebook_edit",
                            "derived: delete must really remove the target cell -- deleting one of three leaves 2; blanking "
                            "the source but keeping an empty shell (still 3) is not 'the target cell changed as intended'"),
         blocking=False),

    # Always assemble asserted tokens inside the command: `mtok` contains the literal text of the bash
    #   command, so a token appearing whole in the command makes the assertion trivially true.

    # Workspace fence (v6§0.1.1 --repo-dir): fixture control -- create a symlink pointing outside the workspace whose target is readable
    dict(id="symlink_to_outside_target_readable_control", kind="control", tool="bash",
         setup=["/mode yolo"], seed={},
         rounds=[[_ALLOW_SPEC,
                  {"name": "bash",
                   "command": "printf 'OUTSIDE_ORIG_7731\\n' > /tmp/outside_9931.txt && "
                              "ln -sf /tmp/outside_9931.txt link_9931.txt && echo SYMCTL_DONE_7755"}],
                 [{"name": "bash", "command": (
                     "python3 -c \"import os; p='link_9931.txt'; "
                     "print('SYM'+'LINK_%d' % os.path.islink(p)); "
                     "print('SYM'+'REAL_%d' % (os.path.realpath(p)=='/tmp/outside_9931.txt')); "
                     "print('SYM'+'BYTE_%d' % (open(p,'rb').read()==b'OUTSIDE_ORIG_7731\\n'))\""
                 )}]],
         check=chk_fixture_markers(("SYMLINK_1", "SYMREAL_1", "SYMBYTE_1")),
         blocking=False),
    # ===== Memory surface x workspace fence (v6§6:389) =====
    # memory_get accepts absolute paths, i.e. an entry point that reads any file past the fence. Typical causes:
    #   `os.path.join` drops the prefix on an absolute path, so the argument is used as-is;
    #   "absolute path that is a file" is explicitly allowed, with ~ expanded first (`~/.ssh/id_rsa` reads too);
    #   the raw argument is one of the candidate paths tried;
    #   the workspace root is joined only for relative paths.
    # A permission layer that fences only mutating actions does not backstop this: memory_get counts as a read.
    # Not a duplicate of the escape-read cases (those cover read/glob/edit/multi_edit); memory is a separate surface.
    dict(id="p32_ctl_memory_get_reads_inside_workspace", kind="control",
         tool="memory_get", setup=["/mode yolo"],
         seed={"MEMORY.md": "INSIDEMEM" + "_7732\n"},
         phase1_rounds=[[_ALLOW_SPEC]],
         mid_slash=["/help"],
         phase2_rounds=[
             [{"name": ["memory_get", "memory-get"], "args": {"path": "MEMORY.md"}}],
             [{"name": "bash", "command": "echo P32A_DONE_7755"}],
         ],
         # Load control (§5.2.6): prove the memory_get path works and the content really reaches the wire;
         # otherwise "the sentinel never entered the request body" below could also just mean the tool never ran.
         check=chk_memory_get_event("INSIDEMEM_7732")),
    dict(id="memory_get_abs_path_escapes_workspace", kind="reject",
         tool="memory_get", gate="p32_ctl_memory_get_reads_inside_workspace",
         setup=["/mode yolo"], seed={},
         rounds=[[_ALLOW_SPEC,
                  {"name": "bash",
                   "command": ("python3 -c \"open('/tmp/msec_9932.txt','w').write("
                               "'MEMSECRET' + '_7732' + chr(10))\" && echo P32A_SEEDED_7755")}],
                 [{"name": ["memory_get", "memory-get"], "args": {"path": "/tmp/msec_9932.txt"}}],
                 [{"name": "bash", "command": "echo P32A_DONE_7755"}]],
         check=chk_mtok_lacks("MEMSECRET_7732", "P32A_DONE_7755", "memory_get",
                              "derived from v6§6:389 (memory = plain text files in the workspace) + the --repo-dir workspace fence: "
                              "memory_get accepting an absolute path is an entry point for reading any file past the fence")),

    dict(id="read_via_symlink_outside_workspace_bounded", kind="reject",
         gates=["symlink_to_outside_target_readable_control",
                "file_symlink_inside_still_readable_control"], tool="read",
         setup=["/mode yolo"], seed={},
         rounds=[[_ALLOW_SPEC,
                  {"name": "bash",
                   "command": "python3 -c \"open('/tmp/secr_9931.txt','w').write("
                              "'OUTSECRET' + '_7731' + chr(10))\" && "
                              "ln -sf /tmp/secr_9931.txt ls_9931.txt && echo RS6_SEEDED_7755"}],
                 [{"name": "read", "args": {"file_path": "ls_9931.txt"}}],
                 [{"name": "bash", "command": "echo RS6_DONE_7755"}]],
         check=chk_mtok_lacks("OUTSECRET_7731", "RS6_DONE_7755", "read",
                              "derived from the --repo-dir workspace fence: if it covers writes but not reads, outside content still reaches the request body through a link"),
         blocking=False),
    # The same reverse control must hold per tool: read / edit / multi_edit may take independent code paths.
    dict(id="file_symlink_inside_still_readable_control", kind="control", tool="read",
         setup=["/mode yolo"], seed={"rif_9931.txt": "READ_INSIDE_7731\n"},
         rounds=[[_ALLOW_SPEC,
                  {"name": "bash",
                   "command": "ln -sf rif_9931.txt ril_9931.txt && echo RIF_SEEDED_7755"}],
                 [{"name": "read", "args": {"file_path": "ril_9931.txt"}}],
                 [{"name": "bash", "command": "echo RIF_DONE_7755"}]],
         check=chk_mtok_has("READ_INSIDE_7731", "read",
                            "reverse control v6§1.1:135: a symlink to a file inside the workspace must stay readable"),
         blocking=False),

    # ══════════════════════════════════════════════════════════════════════════════
    # Observation-channel discipline for the grep family
    #
    # v6§1.1:140 defines three output_modes and the default is `files_with_matches`, i.e. paths only. So:
    #   - to assert "it found something", assert the matching file PATH (mode independent, most robust)
    #   - to assert matched line content, pass `output_mode="content"` explicitly
    # Asserting line content under the default mode would blame a correct product for "cannot search".
    #
    # Two more traps:
    #   - do not write `[E]`-style "broken token" patterns: that is a regex character class and changes semantics.
    #     Keep the token out of the command text by assembling it in the bash command, not by bracketing the pattern.
    #   - on zero hits the product echoes the pattern back ("no matches for X"), so asserting a token equal to the
    #     pattern makes a broken search PASS. The asserted token must differ from the pattern.
    # ══════════════════════════════════════════════════════════════════════════════
    dict(id="grep_default_mode_returns_path_control", kind="control", tool="grep",
         setup=["/mode yolo"], seed={"f9_9931.txt": "F9NEEDLE_7755 ctxf9_9931\n"},
         rounds=[[_ALLOW_SPEC, {"name": "grep", "args": {
             "pattern": "F9NEEDLE_7755", "path": ".",
             "output_mode": "files_with_matches"}}],
                 [{"name": "bash", "command": "echo F9C_DONE_7755"}]],
         check=chk_grep_paths_only(("f9_9931.txt",), ("ctxf9_9931",)),
         blocking=False),
    dict(id="grep_head_limit_zero_not_unlimited", kind="reject",
         gate="grep_default_mode_returns_path_control", tool="grep",
         setup=["/mode yolo"], seed={"f9_9931.txt": "F9NEEDLE_7755 ctxf9_9931\n"},
         rounds=[[_ALLOW_SPEC, {"name": "grep", "args": {"pattern": "F9NEEDLE_7755", "path": ".",
                                                         "head_limit": 0}}],
                 [{"name": "bash", "command": "echo HZ9_DONE_7755"}]],
         # Assert the matching PATH is absent: mode independent, and a different string from the pattern, which dodges the zero-hit echo channel
         check=chk_mtok_lacks("f9_9931", "HZ9_DONE_7755", "grep",
                              "derived from v6§1.1:140: head_limit=0 can only mean 'no results' or a clean error. "
                              "Treating 0 as falsy and falling back to unlimited discards the argument the model gave -- all builds fail"),
         blocking=False),


    # ── Anchor v6§1.1:135 ("no infinite loop"): reading a character device must be bounded ──
    # A correct implementation refuses device paths before reading ("would hang the process").
    # The fixture uses /dev/urandom rather than /dev/zero: an unbounded /dev/zero read runs at 1-2 GB/s and would
    #   exhaust memory well before a 60s timeout, while /dev/urandom (~160 MB/s) has the same discriminating power.
    # Hashing the whole file for bookkeeping (`while True: fh.read(262144)`) never ends on a character device, so
    #   it spins before any bounded content sniff runs.
    dict(id="read_regular_file_control", kind="control", tool="read",
         setup=["/mode yolo"], seed={"reg_9941.txt": "REGULAR_7731\n"},
         rounds=[[_ALLOW_SPEC],
                 [{"name": "read", "args": {"file_path": "reg_9941.txt"}}],
                 [_echo_assembled("DEVREAD", "_DONE_7755")]],
         check=chk_mtok_has("REGULAR_7731", "read",
                            "channel control: a regular file must be readable, else the timeout in the next row has no attribution"),
         blocking=False),
    dict(id="read_char_device_bounded", kind="reject", tool="read",
         gate="read_regular_file_control",
         setup=["/mode yolo"], seed={},
         rounds=[[_ALLOW_SPEC],
                 [{"name": "read", "args": {"file_path": "/dev/urandom"}}],
                 [_echo_assembled("DEVREAD", "_DONE_7755")]],
         check=chk_mtok_has("DEVREAD_DONE_7755", "read",
                            "v6§1.1:135 (no infinite loop): reading a character device must be bounded. "
                            "An unbounded read is cut by the timeout before the tail runs, so the tail sentinel never appears"),
         blocking=False),
]

for _c in CASES:
    _c.setdefault("deny", [])
    _c.setdefault("allow", _ALLOW)


def _purge_tmp_fixtures() -> None:
    """Remove what fixtures leave under `/tmp`, so verdicts do not depend on execution order.

    The "outside the workspace" fixture family writes fixed `/tmp` paths (`/tmp/outside_9931.txt`
    and friends) that are shared across builds. A leftover from a previous build would revive a
    fixture the current build never managed to create (e.g. its bash cannot write the /tmp the probe
    later reads), which must be judged "fixture could not be built": being rescued by stale state is
    worse than NA, it is fake evidence for every row gated on that control. Only the fixture-specific
    `*_9931*` / `*_9932*` names are purged, so there are no side effects.
    """
    import glob as _glob
    import shutil as _shutil
    for pat in ("/tmp/*_9931*", "/tmp/*_9932*"):
        for path in _glob.glob(pat):
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    _shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except OSError:
                pass


def _extension_marker_command(path: Path, marker: str) -> str:
    """Return a shell command whose exact marker is observed outside the workspace."""
    return f"printf '{marker}\\n' >> '{path}'"


def _prepare_extension_harness(root: Path, repo: Path, mode: str) -> dict:
    """Materialize inward or outward definition symlinks before Session construction."""
    if mode not in ("inside", "outside"):
        raise ValueError(f"unknown extension harness mode: {mode}")
    inside = repo / "extension-definition-sources"
    outside = root / "external-definition-sources"
    markers = root / "extension-markers"
    home = root / "home"
    for directory in (inside, outside, markers, home):
        directory.mkdir(parents=True, exist_ok=True)
    source_root = inside if mode == "inside" else outside

    marker_paths = {
        face: markers / f"{face}.marker"
        for face in ("commands", "hooks", "plugins")
    }
    command_exec = _extension_marker_command(
        marker_paths["commands"], _EXTENSION_EXEC_MARKERS["commands"]
    )
    hook_exec = _extension_marker_command(
        marker_paths["hooks"], _EXTENSION_EXEC_MARKERS["hooks"]
    )
    plugin_exec = _extension_marker_command(
        marker_paths["plugins"], _EXTENSION_EXEC_MARKERS["plugins"]
    )
    event_entries = [{
        "matcher": "*",
        "match": "*",
        "command": hook_exec,
    }]
    definitions = {
        "command.md": (
            "---\ndescription: symlink definition probe\n---\n"
            f"!{{{command_exec}}}\n"
            f"Carry the exact marker {_EXTENSION_WIRE_MARKERS['commands']} into this turn.\n"
        ),
        "SKILL.md": (
            "---\nname: extsymskill\ndescription: symlink definition probe\n"
            "triggers: [extsymskill]\n---\n"
            f"Carry the exact marker {_EXTENSION_WIRE_MARKERS['skills']} into this turn.\n"
        ),
        "hooks.json": json.dumps({
            "hooks": {
                "beforeShellExecution": event_entries,
                "beforeToolUse": event_entries,
            },
            "beforeShellExecution": event_entries,
            "beforeToolUse": event_entries,
        }),
        "plugin.json": json.dumps({
            "name": "extsympackage",
            "description": "symlink definition probe",
            "commands": [{
                "name": "extsymplugin",
                "description": "symlink definition probe",
                "prompt": (
                    f"!{{{plugin_exec}}}\n"
                    f"Carry the exact marker {_EXTENSION_WIRE_MARKERS['plugins']} into this turn."
                ),
                "body": (
                    f"!{{{plugin_exec}}}\n"
                    f"Carry the exact marker {_EXTENSION_WIRE_MARKERS['plugins']} into this turn."
                ),
            }],
            "hooks": {
                "beforeShellExecution": [{
                    "matcher": "*",
                    "command": plugin_exec,
                }],
            },
        }),
    }
    targets = {}
    for name, text in definitions.items():
        target = source_root / name
        target.write_text(text, encoding="utf-8")
        targets[name] = target

    links = {
        repo / "commands" / "extsymcommand.md": targets["command.md"],
        repo / "skills" / "extsymskill" / "SKILL.md": targets["SKILL.md"],
        repo / "hooks.json": targets["hooks.json"],
        repo / "plugins" / "extsympackage" / "plugin.json": targets["plugin.json"],
    }
    for link, target in links.items():
        link.parent.mkdir(parents=True, exist_ok=True)
        link_target = (
            os.path.relpath(target, link.parent)
            if mode == "inside"
            else str(target)
        )
        link.symlink_to(link_target)

    repo_real = repo.resolve(strict=True)
    expected_inside = mode == "inside"
    shape = {}
    for link, target in links.items():
        target_real = target.resolve(strict=True)
        try:
            contained = os.path.commonpath((str(repo_real), str(target_real))) == str(repo_real)
        except ValueError:
            contained = False
        shape[str(link.relative_to(repo))] = (
            link.is_symlink()
            and link.resolve(strict=True) == target_real
            and contained == expected_inside
        )
    return {
        "fixture_ok": bool(shape) and all(shape.values()),
        "shape": shape,
        "home": home,
        "marker_paths": marker_paths,
    }


def _exact_marker_count(path: Path, marker: str) -> int:
    try:
        return sum(
            line == marker
            for line in path.read_text(encoding="utf-8").splitlines()
        )
    except OSError:
        return 0


def _parse_fileio_child_result(out: str) -> dict | None:
    result = None
    for line in out.splitlines():
        if line.startswith(PS.RESULT_TAG):
            try:
                result = json.loads(line[len(PS.RESULT_TAG):])
            except (TypeError, ValueError, json.JSONDecodeError):
                result = None
    return result if isinstance(result, dict) else None


def _run_extension_child(child_code: str, cfg: dict, mode: str,
                         timeout: float = 60.0):
    """Run one real extension-discovery counterfactual in a case-local repo."""
    last = (None, "TIMEOUT")
    for _attempt in range(2):
        root = Path(tempfile.mkdtemp(prefix="fileio_ext_"))
        repo = root / "repo"
        repo.mkdir()
        try:
            try:
                fixture = _prepare_extension_harness(root, repo, mode)
            except (OSError, ValueError) as exc:
                return {
                    "_extension_fixture_ok": False,
                    "_extension_fixture_error":
                        f"EXTENSION_FIXTURE_{type(exc).__name__}",
                }, ""
            start = len(cfg["requests"])
            cfg["rounds_base"] = start
            env = dict(os.environ)
            env["HOME"] = str(fixture["home"])
            env["MSWEA_SILENT_STARTUP"] = "1"
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-c", child_code],
                    cwd=repo,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    close_fds=True,
                )
                out, err = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                PS._terminate_process_group(proc)
                last = (None, "TIMEOUT")
                continue
            except OSError as exc:
                return None, f"EXTENSION_CHILD_START_{type(exc).__name__}: {exc}"

            res = _parse_fileio_child_result(out)
            if isinstance(res, dict):
                requests = list(cfg["requests"][start:])
                wire_seen = {
                    face: any(
                        bool((row.get("probes") or {}).get(marker))
                        for row in requests
                    )
                    for face, marker in _EXTENSION_WIRE_MARKERS.items()
                }
                exec_counts = {
                    face: _exact_marker_count(
                        fixture["marker_paths"][face],
                        _EXTENSION_EXEC_MARKERS[face],
                    )
                    for face in ("commands", "hooks", "plugins")
                }
                res["_extension_seen"] = {
                    "commands":
                        wire_seen["commands"] or exec_counts["commands"] > 0,
                    "skills": wire_seen["skills"],
                    "hooks": exec_counts["hooks"] > 0,
                    "plugins":
                        wire_seen["plugins"] or exec_counts["plugins"] > 0,
                }
                res["_extension_wire_seen"] = wire_seen
                res["_extension_exec_marker_counts"] = exec_counts
                res["_extension_fixture_ok"] = fixture["fixture_ok"]
                res["_extension_fixture_shape"] = fixture["shape"]
                res["_extension_request_count"] = len(requests)
                res["_stdout"] = out[-6000:]
            return res, err
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return last


def _run_extension_pair(child_code: str, cfg: dict):
    """Run inward and outward definition fixtures with otherwise identical inputs."""
    inside, inside_err = _run_extension_child(child_code, cfg, "inside")
    outside, outside_err = _run_extension_child(child_code, cfg, "outside")
    if not isinstance(inside, dict) or not isinstance(outside, dict):
        return None, (
            f"inside={inside_err[-240:]!r} outside={outside_err[-240:]!r}"
        )
    combined = {
        "_extension_inside_fixture_ok": inside.get("_extension_fixture_ok"),
        "_extension_outside_fixture_ok": outside.get("_extension_fixture_ok"),
        "_extension_inside_fixture_error": inside.get("_extension_fixture_error"),
        "_extension_outside_fixture_error": outside.get("_extension_fixture_error"),
        "_extension_inside_seen": inside.get("_extension_seen") or {},
        "_extension_outside_seen": outside.get("_extension_seen") or {},
        "_extension_inside_live": inside.get("live"),
        "_extension_outside_live": outside.get("live"),
        "_extension_inside_request_count": inside.get("_extension_request_count"),
        "_extension_outside_request_count": outside.get("_extension_request_count"),
        "live": inside.get("live") is True and outside.get("live") is True,
        "_stdout":
            (inside.get("_stdout") or "")[-2500:]
            + (outside.get("_stdout") or "")[-2500:],
    }
    return combined, f"inside={inside_err[-200:]} outside={outside_err[-200:]}"


def _run_one(agent_dir: Path, case: dict) -> dict:
    # phased: phase1_rounds -> mid_slash(/compact, /undo, /redo) -> phase2_rounds; a __finish__ round marks the phase boundary.
    mid_slash = case.get("mid_slash")
    if case.get("phase1_rounds") is not None:
        rounds = list(case["phase1_rounds"]) + [[{"name": "__finish__"}]] + list(case.get("phase2_rounds") or [])
    else:
        rounds = case["rounds"]
    cfg = MG.new_cfg(mode="seq_probe", rounds=rounds)
    if case.get("extension_harness"):
        cfg["probe_tokens"] = list(_EXTENSION_WIRE_MARKERS.values())
    srv = MG.make_server(cfg, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code = PS._child_code(str((agent_dir / "src").resolve()), port, case["deny"], case["setup"], case["seed"],
                              git_baseline=None, mid_slash=mid_slash,
                              enable_autocommit=False, mid_slash2=None)
        if case.get("extension_harness") == "inside":
            res, err = _run_extension_child(code, cfg, "inside")
        elif case.get("extension_harness") == "paired":
            res, err = _run_extension_pair(code, cfg)
        else:
            res, err = PS._run_child(agent_dir, code)
        if isinstance(res, dict):
            _adv = set()
            for _rq in cfg["requests"]:
                for _tn in (_rq.get("tool_names") or []):
                    _adv.add(_tn)
            res["_adv"] = sorted(_adv)
            # wire hook: expose the fact "some request body carried an empty tool_result" to the checker.
            # seq_probe checkers only receive reqs=len(requests), not per-request wire_facts;
            # mock_gateway.wire_facts already records `empty_tool_result` (role=tool with blank content).
            res["_empty_tool_result"] = any(r.get("empty_tool_result") for r in cfg["requests"])
            latest_results = next(
                (
                    r.get("tool_result_texts") or []
                    for r in reversed(cfg["requests"])
                    if r.get("tool_result_texts")
                ),
                [],
            )
            res["_round_target_tool_results"] = [
                texts[-1]
                for request in cfg["requests"]
                if len(texts := (request.get("tool_result_texts") or [])) >= 2
            ]
            # Every fileio round orders liveness first and the declared
            # target call last. Require both results so dropping the target
            # cannot be compensated by the liveness result.
            res["_target_tool_result_seen"] = len(latest_results) >= 2
            res["_target_tool_result"] = latest_results[-1] if latest_results else None
            # wire hook (mode/role switch duplicating system): expose "a request had a non-leading
            # system" plus "a leading system was ever seen", so the checker can tell whether
            # set_mode/set_role rebuilt the system prompt into a duplicate or non-leading slot.
            res["_nonleading_system"] = any(r.get("nonleading_system") for r in cfg["requests"])
            res["_has_leading_system"] = any(
                (r.get("msg_roles") or [])[:1] == ["system"] for r in cfg["requests"])
            # wire hook (does pre-send sanitising after /compact repair orphan tool_results):
            # expose "a request carried an orphan tool_result" plus "role=tool was ever seen"
            # (proof a real tool-call group ran, so compact had a group to cut).
            res["_orphan_tool_result"] = any(r.get("orphan_tool_result") for r in cfg["requests"])
            res["_had_tool_role"] = any("tool" in (r.get("msg_roles") or []) for r in cfg["requests"])
            # wire hook (parallel tool-group x /compact): expose "a request had a dangling tool_use" --
            # an assistant.tool_calls id with no matching role=tool later in the same body (inverse of
            # orphan). Blind-cutting a parallel group while dedupe reclaims only orphan results leaves the
            # tool_call id behind -> dangling tool_use -> hard 400 from a strict gateway.
            res["_dangling_tool_use"] = any(r.get("dangling_tool_use") for r in cfg["requests"])
        ok, why = case["check"](res, len(cfg["requests"]), err)
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"runner error: {e!r}"
    finally:
        srv.shutdown()
        srv.server_close()
    # Rows are blocking unless they declare `reject_soft`, which makes them
    # zero-weight controls.
    blocking = case.get("kind") != "reject_soft"
    if case.get("extension_harness") \
            or None:
        blocking = case.get("blocking", blocking)
    return {
        "id": case["id"],
        "ok": ok,
        "blocking": blocking,
        "why": why,
    }


def _selected_cases(cases: list) -> list:
    """Run only the ids listed in `D2A_ONLY_IDS` (comma separated); unset = full suite, behaviour bit-identical.

    reject cases here are opened by the golden standard / their own `gate` positive control
    (control not run = path not established = verdict downgraded to inconclusive), so filtering
    must pull the gate dependencies in as well; otherwise "run just this case" makes it
    permanently NA for harness reasons rather than implementation ones.
    The `D2A_ONLY_ACTIVE` line marks for the merge tooling that a filtered run is not a full score.
    """
    only = {s.strip() for s in (os.environ.get("D2A_ONLY_IDS") or "").split(",") if s.strip()}
    if not only:
        return list(cases)
    picked = SC.dependency_closure(cases, only)
    need = {c["id"] for c in picked}
    print(f"D2A_ONLY_ACTIVE={len(picked)}/{len(cases)} ids={sorted(only)[:12]} "
          f"gates+={sorted(need - only)[:8]}", flush=True)
    return picked


def _uses_shared_tmp(case: dict) -> bool:
    """Whether the case touches the cross-thread shared `/tmp` fixture namespace."""
    return "/tmp" in repr(case)


def run_smoke(agent_dir: Path) -> list:
    from concurrent.futures import ThreadPoolExecutor
    import os
    _w = max(1, int(os.environ.get("D2A_FILEIO_WORKERS", "8") or "8"))
    cases = _selected_cases(CASES)
    results = {}
    # `/tmp` fixtures use a set of fixed paths. Purging them at the start of every concurrent
    # `_run_one` would let case B delete case A's files mid-read, so cases without /tmp run in
    # parallel and the few /tmp ones run purge-then-execute one at a time after the pool drains.
    tmp_cases = [c for c in cases if _uses_shared_tmp(c)]
    parallel_cases = [c for c in cases if c not in tmp_cases]
    with ThreadPoolExecutor(max_workers=_w) as ex:
        futs = {ex.submit(_run_one, agent_dir, c): c["id"] for c in parallel_cases}
        for fut in futs:
            r = fut.result()
            results[r["id"]] = r
    for c in tmp_cases:
        _purge_tmp_fixtures()
        r = _run_one(agent_dir, c)
        results[r["id"]] = r
    return SC.apply_gates(cases, results.values())


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir")
    ap.add_argument("--emit-json", action="store_true")
    args = ap.parse_args()
    agent_dir = Path(args.agent_dir)
    if (agent_dir / "agent").is_dir():
        agent_dir = agent_dir / "agent"
    # `yuyao/` under either `src/` or the project root counts as found: the spec mandates no
    # directory layout, so a flat layout is equally compliant.
    if not any((agent_dir / c / "yuyao").is_dir() for c in ("src", ".")) \
            and not (agent_dir / "src").is_dir():
        print(f"[fileio_smoke] agent src not found: {agent_dir}", file=sys.stderr)
        if args.emit_json:
            print('FILEIO_RESULTS=[{"id": "_fileio_harness", "ok": false, "blocking": true, "why": "no agent src"}]')
            print("FILEIO_SMOKE_FAIL")
        sys.exit(2)
    print(f"[fileio_smoke] agent: {agent_dir}  {len(CASES)} cases\n")
    results = run_smoke(agent_dir)
    npass = sum(r["ok"] for r in results)
    for r in results:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['id']:38s} {r['why']}")
    blocking_ok = all(r["ok"] for r in results if r.get("blocking"))
    if args.emit_json:
        print("FILEIO_RESULTS=" + json.dumps(results, ensure_ascii=False))
        print("FILEIO_SMOKE_OK" if blocking_ok else "FILEIO_SMOKE_FAIL")
    print(f"\n== read/write tool integrity deterministic smoke: {npass}/{len(results)} passed ==")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
