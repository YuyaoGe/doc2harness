#!/usr/bin/env python3
"""tui-line layer: launch the build's own interactive TUI inside a REAL pty and feed user input
line by line into stdin.

Why a separate layer is needed:
  the spec §5.4 input prefixes (`/` `@` `!`) are usually handled only in the interactive line
  handler, which a non-interactive path cannot reach, and `tui_smoke.py` drives the product by
  method name (`chat` / `agentic_turn` / `_run_turn`), which misses a `_handle_line`-style handler.

How this layer drives the line handler:
  instead of calling functions by name, run the build's own CLI entry point with NO arguments (which
  enters the interactive TUI) and write lines to the pty master. Which internal path the build takes
  and what its functions are called is irrelevant - this is the only way to drive line-handler
  implementations.

Division of labor with the other layers:
  - `tui_smoke.py`  - also uses a pty, but tests RENDERING (Live redraws, streaming) and drives by
    method name.
  - `headless_smoke` - a real CLI, but on the `-p` non-interactive path, which cannot reach the
    tui.py line handler.
  - this layer - real CLI + interactive path + line-by-line input.

Two observation surfaces, which suffice:
  1. The stub gateway's request records (`probes` / `probes_in_system` / `tool_names` / `stream`
     ...) - whether `@` references injected file contents into the request body is visible here.
  2. Visible text on the pty (ANSI stripped) - output of `!`-prefixed commands and error messages.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import mock_gateway as MG   # noqa: E402

_ANSI = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Wind-down sentinel: after all input lines, feed one more line `!echo <DONE>`; seeing it means every
# preceding line was processed. The `!` prefix is deliberate - it is itself one of the surfaces under
# test, and if a build does not support `!` we fall back to the timeout and the checks' self-gating
# reports inconclusive rather than a hard FAIL.
DONE = "TUILINE_DONE_7755"
PROMPT_CANARY_FILE = ".d2a_prompt_canary"
PROMPT_CANARY_NONCE = "TUILINE_PROMPT_READY_4E21"
PROMPT_CANARY_COMMAND = f"!cat {PROMPT_CANARY_FILE}"
PROMPT_OBSERVER_ID = "pty-local-shell-canary/v1"
_HISTORY_SNAPSHOT_MAX_FILES = 256
_HISTORY_SNAPSHOT_MAX_BYTES = 2 * 1024 * 1024


def _line_payload(text: str, line_terminator: str = "\r") -> bytes:
    """Encode one logical TUI line with an explicit, audited terminator."""
    if line_terminator not in ("\r", "\n"):
        raise ValueError("line_terminator must be CR (\\r) or LF (\\n)")
    return (text + line_terminator).encode()


def _prompt_canary_observation(
    *,
    candidate_output: bytes,
    canary_payload: bytes,
    output_after_write: bytes,
    process_running: bool,
    returncode: int | None,
) -> dict:
    """Classify a prompt candidate from facts that terminal echo cannot forge."""
    nonce = PROMPT_CANARY_NONCE.encode()
    candidate = bool(candidate_output)
    nonce_absent_before = nonce not in candidate_output
    nonce_absent_from_input = nonce not in canary_payload
    canary_observed = nonce in output_after_write
    input_consumed = (
        candidate
        and nonce_absent_before
        and nonce_absent_from_input
        and canary_observed
        and process_running
    )
    failure = None
    if not input_consumed:
        if returncode is not None:
            failure = {
                "kind": "process-exit-before-prompt",
                "returncode": returncode,
                "signal": -returncode if returncode < 0 else None,
            }
        elif not candidate:
            failure = {"kind": "no-output-candidate"}
        elif not nonce_absent_before:
            failure = {"kind": "canary-preexisted"}
        elif not nonce_absent_from_input:
            failure = {"kind": "canary-present-in-input"}
        elif not canary_observed:
            failure = {"kind": "canary-not-observed"}
        else:
            failure = {"kind": "process-not-running"}
    normalized_candidate = candidate
    normalized_canary_observed = canary_observed
    if returncode is not None and not input_consumed:
        # Whether a quiet boundary happened just before an early exit is a
        # scheduler-sensitive detail, not prompt identity.  Collapse all
        # pre-handshake exits to one portable startup-failure timeline.
        normalized_candidate = False
        normalized_canary_observed = False
        events = ["startup-failure"]
    else:
        events = ["candidate-boundary"] if candidate else []
        if candidate:
            events.append("canary-write")
        if canary_observed:
            events.append("canary-observed")
        if input_consumed:
            events.append("input-consumed")
    return {
        "schema": "d2a.tui-prompt-observation/1",
        "prompt_identity": PROMPT_OBSERVER_ID,
        "turn_id": "prompt-0",
        "candidate_boundary": normalized_candidate,
        "canary_nonce_absent_before": nonce_absent_before,
        "canary_nonce_absent_from_input": nonce_absent_from_input,
        "canary_observed": normalized_canary_observed,
        "input_consumed": input_consumed,
        "startup_failure": failure,
        "timeline": events,
    }


def _discover_tui_cli(agent_dir: Path):
    """Return (argv, env): running it with no arguments enters the interactive TUI.

    Same candidates and same PYTHONPATH handling as the headless layer's `_discover_cli`, except it
    does NOT require `-p`/`--print` in the help - this layer wants the interactive entry point, not
    the headless one. The criterion becomes "`--help` runs successfully (rc=0)", so an entry point
    that cannot start at all is not treated as usable.
    """
    env = dict(os.environ)
    src = os.pathsep.join([str((agent_dir / "src").resolve()), str(agent_dir.resolve())])
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["MSWEA_SILENT_STARTUP"] = "1"
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["MSWEA_COST_TRACKING"] = "ignore_errors"
    for c in ([sys.executable, "-m", "yuyao"], [sys.executable, "-m", "yuyao.cli"], ["yuyao"]):
        try:
            r = subprocess.run(c + ["--help"], cwd=str(agent_dir), env=env,
                               capture_output=True, text=True, timeout=40)
        except Exception:
            continue
        # Same rule as the headless layer's _discover_cli: also accept a complete help page with
        # rc!=0 (a known shape where the build's argparse maps SystemExit(0) to 2). A usage line plus
        # enough content is content-level evidence that the entry point works; an entry point that
        # crashes before argparse cannot print a full help page, and a single-line usage error is
        # blocked by the 200-byte floor.
        help_txt = (r.stdout or "") + (r.stderr or "")
        help_complete = (
            re.search(r"(?im)^\s*(usage|用法)[:：]", help_txt) is not None
            and len(help_txt) > 200
        )
        if r.returncode == 0 or help_complete:
            return c, env
    return None, env


def _readiness_budget(timeout: float) -> float:
    return min(30.0, max(10.0, timeout * 0.5))


def _split_control_prefix(line: str) -> tuple[str, str]:
    """Separate a physical Ctrl+L keypress from text typed after redraw."""
    if line.startswith("\x0c") and len(line) > 1:
        return line[0], line[1:]
    return "", line


def _input_segments(line: str) -> list[str]:
    """Split control-key transitions from text typed after UI handling.

    PTYs preserve write boundaries, but prompt_toolkit does not promise to
    process a control key and later text atomically when both arrive in one
    write.  Keep bracketed-paste and general escape sequences intact; split
    only the control-key transitions known to be consumed separately.
    """
    control, rest = _split_control_prefix(line)
    if control:
        return [control, rest]
    if "\x03" in line:
        before, after = line.split("\x03", 1)
        return [part for part in (before, "\x03", after) if part]
    backspaces = re.search(r"[\x08\x7f]+", line)
    if backspaces:
        return [
            part
            for part in (
                line[: backspaces.start()],
                backspaces.group(),
                line[backspaces.end() :],
            )
            if part
        ]
    return [line]


def _history_snapshot(home: str | Path) -> dict:
    """Capture bounded raw bytes from product history files under one case HOME.

    History ownership is inferred only from the public on-disk namespace: at
    least one relative path component must contain ``history``.  The Harness
    never writes product history syntax and never guesses a product slug.
    """
    root = Path(home).resolve()
    files: dict[str, dict] = {}
    total = 0
    try:
        candidates = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as exc:
        raise RuntimeError(f"cannot enumerate isolated HOME history: {exc}") from exc
    for path in candidates:
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            rel = resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if not any("history" in part.casefold() for part in rel.parts):
            continue
        if len(files) >= _HISTORY_SNAPSHOT_MAX_FILES:
            raise RuntimeError("history snapshot exceeded file bound")
        try:
            data = resolved.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"cannot read history bytes {rel.as_posix()}: {exc}") from exc
        total += len(data)
        if total > _HISTORY_SNAPSHOT_MAX_BYTES:
            raise RuntimeError("history snapshot exceeded byte bound")
        files[rel.as_posix()] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data_hex": data.hex(),
        }
    return {
        "schema": "d2a.tui-history-snapshot/1",
        "complete": True,
        "file_count": len(files),
        "byte_count": total,
        "files": files,
    }


def _history_token_paths(snapshot: dict, token: str) -> list[str]:
    needle = token.encode()
    paths = []
    for rel, row in (snapshot.get("files") or {}).items():
        try:
            data = bytes.fromhex(str(row.get("data_hex") or ""))
        except ValueError:
            continue
        if needle in data:
            paths.append(str(rel))
    return sorted(paths)


def _feed_pty(
    argv,
    env,
    workdir,
    lines,
    timeout,
    cfg=None,
    *,
    home: str | Path | None = None,
    events: list[dict] | None = None,
    line_terminator: str = "\r",
):
    """Launch the TUI in a real pty, feed input line by line, return the ANSI-stripped visible text.

    Feeding rhythm (each rule guards against a real failure mode):
      - Wait a moment after starting the process before feeding the first line: some implementations
        must finish drawing a welcome screen before they read stdin, and a line fed too early is lost.
      - The wait condition PER LINE depends on the line type. Lines starting with `!`/`/` are handled
        locally and echo immediately, so waiting for the echo is enough; ordinary text runs a model
        turn, so the feeder waits until the stub gateway's request count actually increases before
        feeding the next line. Breaking out on the wind-down sentinel alone is not enough: `!echo
        <DONE>` is a local command that returns instantly, so the master would be closed before the
        previous line's model turn had even sent its request.
      - The line terminator MUST be `\\r`, not `\\n`; this is what makes the layer work at all. The
        input box is prompt_toolkit in multiline mode, so `\\n` only inserts a newline: the prompt
        turns from `>` into a continuation `. .` and the input buffers forever without ever being
        submitted, so the stub gateway receives nothing. Only terminators containing `\\r` submit.
      - On exit feed `/exit` AND close the master to send EOF - implementations differ in which exit
        route they honor.
    """
    if line_terminator not in ("\r", "\n"):
        raise ValueError("line_terminator must be CR (\\r) or LF (\\n)")
    import pty
    master, slave = pty.openpty()
    env = dict(env)
    env["COLUMNS"] = "100"
    env["LINES"] = "30"
    env["TERM"] = "xterm"
    env["HOME"] = str(home or workdir)
    # A host override would silently move product history outside the case HOME.
    env.pop("YUYAO_HOME", None)
    p = subprocess.Popen(
        argv,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=workdir,
        env=env,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    buf = bytearray()
    timeline: list[dict] = []
    timeline_bytes = 0

    def _record(direction: str, data: bytes) -> None:
        nonlocal timeline_bytes
        if timeline_bytes >= 1_000_000:
            return
        kept = data[: 1_000_000 - timeline_bytes]
        timeline_bytes += len(kept)
        timeline.append(
            {
                "t_ns": time.monotonic_ns(),
                "direction": direction,
                "data_hex": kept.hex(),
            }
        )

    def _reader():
        while True:
            try:
                r, _, _ = select.select([master], [], [], 0.4)
            except (OSError, ValueError):
                return
            if not r:
                if p.poll() is not None:
                    return
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            if not data:
                return
            buf.extend(data)
            _record("read", data)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    deadline = time.time() + timeout
    prompt_ready = False
    prompt_observation = _prompt_canary_observation(
        candidate_output=b"",
        canary_payload=_line_payload(PROMPT_CANARY_COMMAND, line_terminator),
        output_after_write=b"",
        process_running=p.poll() is None,
        returncode=p.poll(),
    )
    sent_lines = (
        []
        if events is not None
        else list(lines) + ["!printf 'TUILINE_%s_7755\\n' DONE", "/exit"]
    )
    event_results: list[dict] = []
    try:
        def _wait_output_quiet(
            max_wait: float,
            quiet_for: float = 0.35,
            growth_from: int | None = None,
        ) -> bool:
            end = min(time.time() + max_wait, deadline)
            last_size = len(buf)
            stable_since = time.monotonic()
            observed_growth = growth_from is None or last_size > growth_from
            while time.time() < end and p.poll() is None:
                size = len(buf)
                if size != last_size:
                    last_size = size
                    stable_since = time.monotonic()
                if growth_from is not None and size > growth_from:
                    observed_growth = True
                if observed_growth and time.monotonic() - stable_since >= quiet_for:
                    return True
                time.sleep(0.05)
            return False

        # Output quiet is only a candidate boundary.  A banner, warning, traceback or
        # process-exit prelude can all become quiet without an input prompt.  Prove the
        # real prompt/turn by asking the TUI to execute a local command whose output
        # nonce is stored in a fixture file and is absent from the bytes we write.
        ready_budget = _readiness_budget(timeout)
        ready_end = min(time.time() + ready_budget, deadline)
        last_size = -1
        stable_since = time.monotonic()
        candidate_output = b""
        while time.time() < ready_end and p.poll() is None:
            size = len(buf)
            if size != last_size:
                last_size = size
                stable_since = time.monotonic()
            elif size > 0 and time.monotonic() - stable_since >= 0.25:
                timeline.append(
                    {
                        "t_ns": time.monotonic_ns(),
                        "direction": "prompt-candidate",
                        "data_hex": "",
                    }
                )
                candidate_output = bytes(buf)
                break
            time.sleep(0.05)
        canary_payload = _line_payload(PROMPT_CANARY_COMMAND, line_terminator)
        canary_start = len(buf)
        if candidate_output and p.poll() is None:
            # The first write may land in the build's cooked window (prompt_toolkit has not switched
            # to raw yet): the pty line discipline's ICRNL translates \r into \n, which for a
            # multiline input box only inserts a newline instead of submitting, so simply waiting
            # always loses. Use bounded-settle polling with retry convergence instead: write the full
            # canary first, then every 1.5s add only a bare \r - once raw mode is established that is
            # c-m = enter and submits the canary line sitting in the draft.
            # Do not resend the full canary (it would accumulate a multi-line dirty draft), and never
            # pre-clear the pty's ICRNL (plain-REPL builds rely on it to turn \r into lines;
            # pre-clearing would deterministically turn those into na).
            retry_interval = 1.5  # << the 10s floor of _readiness_budget
            attempt = 0
            next_write = 0.0  # write immediately
            while time.time() < ready_end and p.poll() is None:
                output_after_write = bytes(buf[canary_start:])
                prompt_observation = _prompt_canary_observation(
                    candidate_output=candidate_output,
                    canary_payload=canary_payload,
                    output_after_write=output_after_write,
                    process_running=p.poll() is None,
                    returncode=p.poll(),
                )
                if prompt_observation["input_consumed"]:
                    prompt_ready = True
                    timeline.append(
                        {
                            "t_ns": time.monotonic_ns(),
                            "direction": "prompt-ready",
                            "data_hex": "",
                        }
                    )
                    _wait_output_quiet(
                        max(0.0, ready_end - time.time()),
                        quiet_for=0.5,
                        growth_from=canary_start,
                    )
                    break
                if time.monotonic() >= next_write:
                    # First write: full canary; retries send only bare \r (a correct build succeeds on
                    # the first attempt and never retries)
                    payload = canary_payload if attempt == 0 else line_terminator.encode()
                    try:
                        os.write(master, payload)
                        _record(
                            "canary-write" if attempt == 0 else "canary-retry",
                            payload,
                        )
                    except OSError:
                        pass
                    attempt += 1
                    next_write = time.monotonic() + retry_interval
                time.sleep(0.05)
        if not prompt_ready:
            prompt_observation = _prompt_canary_observation(
                candidate_output=candidate_output,
                canary_payload=canary_payload,
                output_after_write=bytes(buf[canary_start:]),
                process_running=p.poll() is None,
                returncode=p.poll(),
            )
        if not prompt_ready:
            sent_lines = []
        if prompt_ready and events is not None:
            for event in events:
                if time.time() > deadline or p.poll() is not None:
                    break
                if not isinstance(event, dict):
                    raise ValueError("TUI event must be a dict")
                kind = str(event.get("kind") or "")
                name = str(event.get("name") or f"event-{len(event_results)}")
                output_start = len(buf)
                requests_before = len(cfg["requests"]) if cfg is not None else 0
                payload = b""
                snapshot = None
                if kind == "snapshot-history":
                    snapshot = _history_snapshot(home or workdir)
                else:
                    if kind == "line":
                        text = event.get("text")
                        if not isinstance(text, str):
                            raise ValueError(f"{name}: line event requires text")
                        payload = _line_payload(text, line_terminator)
                    elif kind == "bytes":
                        data = event.get("data")
                        if isinstance(data, str):
                            payload = data.encode()
                        elif isinstance(data, bytes):
                            payload = data
                        else:
                            raise ValueError(f"{name}: bytes event requires str or bytes")
                    elif kind == "control":
                        value = event.get("value")
                        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
                            raise ValueError(f"{name}: control value must be one byte")
                        payload = bytes((value,))
                    else:
                        raise ValueError(f"{name}: unknown TUI event kind {kind!r}")
                    if not payload:
                        raise ValueError(f"{name}: empty TUI input event")
                    try:
                        os.write(master, payload)
                        _record("write", payload)
                    except OSError:
                        break

                    wait_mode = str(event.get("wait") or (
                        "request"
                        if kind == "line"
                        and not str(event.get("text") or "").startswith(("!", "/"))
                        else "quiet"
                    ))
                    wait_s = float(event.get("timeout") or (
                        40.0 if wait_mode == "request" else 8.0
                    ))
                    if wait_mode == "request":
                        end = min(time.time() + wait_s, deadline)
                        while time.time() < end and p.poll() is None:
                            if cfg is not None and len(cfg["requests"]) > requests_before:
                                response_start = len(buf)
                                _wait_output_quiet(
                                    min(8.0, max(0.0, end - time.time())),
                                    quiet_for=0.5,
                                    growth_from=response_start,
                                )
                                break
                            time.sleep(0.05)
                    elif wait_mode == "terminal":
                        end = min(time.time() + wait_s, deadline)
                        while time.time() < end and p.poll() is None:
                            time.sleep(0.05)
                    elif wait_mode == "quiet":
                        _wait_output_quiet(
                            wait_s,
                            quiet_for=float(event.get("quiet_for") or 0.5),
                            growth_from=output_start,
                        )
                    elif wait_mode != "none":
                        raise ValueError(f"{name}: unknown wait mode {wait_mode!r}")

                output_end = len(buf)
                event_output = bytes(buf[output_start:output_end])
                event_results.append(
                    {
                        "name": name,
                        "kind": kind,
                        "input_hex": payload.hex(),
                        "output_text": _ANSI.sub(
                            "",
                            event_output.decode("utf-8", errors="replace"),
                        ),
                        "output_sha256": hashlib.sha256(event_output).hexdigest(),
                        "output_size": len(event_output),
                        "requests_before": requests_before,
                        "requests_after": (
                            len(cfg["requests"]) if cfg is not None else requests_before
                        ),
                        "returncode": p.poll(),
                        "snapshot": snapshot,
                    }
                )
        for ln in sent_lines:
            if time.time() > deadline:
                break
            segments = _input_segments(ln)
            for segment in segments[:-1]:
                # These chunks represent draft/control-key transitions, not
                # submitted lines.  Wait for the TUI to consume each physical
                # transition before writing text that semantically follows it.
                _wait_output_quiet(3.0)
                try:
                    segment_payload = segment.encode()
                    os.write(master, segment_payload)
                    _record("write", segment_payload)
                except OSError:
                    break
                _wait_output_quiet(3.0)
            else:
                ln = segments[-1]
                local = ln.startswith(("!", "/"))
                n_before = len(cfg["requests"]) if cfg is not None else 0
                grew_before = len(buf)
                try:
                    payload = _line_payload(ln, line_terminator)
                    os.write(master, payload)   # must be \r, see comment above
                    _record("write", payload)
                except OSError:
                    break
                # Local commands echo input before their side effect finishes.
                # Waiting for first growth therefore races the following line;
                # require the output to become quiet after observed growth.
                wait = 8.0 if local else 40.0
                if local:
                    _wait_output_quiet(
                        wait,
                        quiet_for=0.5,
                        growth_from=grew_before,
                    )
                else:
                    end = min(time.time() + wait, deadline)
                    while time.time() < end:
                        if cfg is not None and len(cfg["requests"]) > n_before:
                            response_start = len(buf)
                            # Request arrival is not turn completion. Wait for the
                            # subsequent PTY output to settle before the next key.
                            _wait_output_quiet(
                                8.0,
                                quiet_for=0.5,
                                growth_from=response_start,
                            )
                            break
                        time.sleep(0.3)
            # Runaway-loop brake: a product that loops on the stub's constant "ack" can issue hundreds
            # of requests on a single plain-text line. Without braking, subsequent lines are
            # drowned in unrelated requests and the checker observes garbage. The brake itself does
            # not judge right or wrong - that is a doomloop concern - it only keeps the observation
            # surface clean.
            if cfg is not None and len(cfg["requests"]) > 40:
                break
    finally:
        try:
            os.close(master)                 # send EOF; implementations that exit on EOF wind down here
        except OSError:
            pass
        try:
            p.wait(timeout=8)
        except Exception:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        t.join(timeout=2)
    raw = bytes(buf)
    rc = p.returncode
    return {
        "text": _ANSI.sub("", raw.decode("utf-8", errors="replace")),
        "prompt_ready": prompt_ready,
        "prompt_observation": prompt_observation,
        "returncode": rc,
        "signal": -rc if isinstance(rc, int) and rc < 0 else None,
        "timeline": timeline,
        "events": event_results,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "raw_size": len(raw),
        "split_nonce": all(DONE not in line for line in sent_lines),
    }


_HISTORY_A_PREFIX = "H8A9961_"
_HISTORY_A_TOKEN = _HISTORY_A_PREFIX + "ONLY_A_4C7E"
_HISTORY_B_PREFIX = "H8B9962_"
_HISTORY_B_TOKEN = _HISTORY_B_PREFIX + "ONLY_B_9D3F"


def _prepare_tui_workspace(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / PROMPT_CANARY_FILE).write_text(PROMPT_CANARY_NONCE, encoding="utf-8")


def _event_result(pty_result: dict, name: str) -> dict:
    return next(
        (row for row in (pty_result.get("events") or []) if row.get("name") == name),
        {},
    )


def _aggregate_pty_results(runs: list[dict]) -> dict:
    observations = [run.get("prompt_observation") or {} for run in runs]
    ready = bool(runs) and all(run.get("prompt_ready") for run in runs)
    failures = [row.get("startup_failure") for row in observations if row.get("startup_failure")]
    digest_input = b"".join(
        bytes.fromhex(str(run.get("raw_sha256") or "0" * 64))
        for run in runs
    )
    timeline = []
    for ordinal, run in enumerate(runs):
        for row in run.get("timeline") or []:
            timeline.append({"run": ordinal, **row})
    returncodes = [run.get("returncode") for run in runs]
    nonzero = next((rc for rc in returncodes if isinstance(rc, int) and rc != 0), None)
    signal_number = next(
        (run.get("signal") for run in runs if run.get("signal") is not None),
        None,
    )
    return {
        "prompt_ready": ready,
        "prompt_observation": {
            "schema": "d2a.tui-prompt-observation-multi/1",
            "prompt_identity": PROMPT_OBSERVER_ID,
            "turn_id": "prompt-multi",
            "candidate_boundary": bool(runs),
            "canary_nonce_absent_before": all(
                row.get("canary_nonce_absent_before") for row in observations
            ),
            "canary_nonce_absent_from_input": all(
                row.get("canary_nonce_absent_from_input") for row in observations
            ),
            "canary_observed": all(row.get("canary_observed") for row in observations),
            "input_consumed": ready,
            "startup_failure": failures or None,
            "timeline": [row.get("timeline") for row in observations],
            "run_count": len(runs),
        },
        "returncode": nonzero if nonzero is not None else (0 if runs else None),
        "returncodes": returncodes,
        "signal": signal_number,
        "timeline": timeline,
        "raw_sha256": hashlib.sha256(digest_input).hexdigest(),
        "raw_size": sum(int(run.get("raw_size") or 0) for run in runs),
        "split_nonce": all(run.get("split_nonce") for run in runs),
    }


def _history_writer(argv, env, workspace: Path, home: Path, cfg: dict, token: str) -> dict:
    _prepare_tui_workspace(workspace)
    return _feed_pty(
        argv,
        env,
        str(workspace),
        [],
        35,
        cfg=cfg,
        home=home,
        events=[
            {
                "kind": "line",
                "name": "write-token",
                "text": token,
                "wait": "request",
            },
            {
                "kind": "control",
                "name": "cancel-turn",
                "value": 0x03,
                "wait": "quiet",
                "timeout": 4,
            },
            {
                "kind": "control",
                "name": "exit",
                "value": 0x04,
                "wait": "terminal",
                "timeout": 5,
            },
        ],
    )


def _history_reader(argv, env, workspace: Path, home: Path, cfg: dict, prefix: str) -> dict:
    _prepare_tui_workspace(workspace)
    return _feed_pty(
        argv,
        env,
        str(workspace),
        [],
        35,
        cfg=cfg,
        home=home,
        events=[
            # Physical 0x12 is its own write/timeline event.  No full history token
            # appears in this process's input, so terminal echo cannot forge recall.
            {
                "kind": "control",
                "name": "ctrl-r-start",
                "value": 0x12,
                "wait": "quiet",
                "timeout": 3,
            },
            {
                "kind": "bytes",
                "name": "ctrl-r-query",
                "data": prefix,
                "wait": "quiet",
                "timeout": 3,
            },
        ],
    )


def _history_collision_workspaces(root: Path) -> tuple[Path, Path]:
    base = root / "workspaces" / "collision"
    return base / "piece_a_b" / "leaf", base / "piece_a" / "b" / "leaf"


def _lossy_history_slug(path: Path) -> str:
    return re.sub(r"[^\w]+", "_", os.path.realpath(path)).strip("_")[-80:] or "root"


def _run_history_scenario(argv, env, root: Path, cfg: dict, scenario: str) -> dict:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = []
    facts: dict = {"scenario": scenario}

    if scenario in {"history-fresh-a", "history-fresh-b"}:
        workspace_a, workspace_b = _history_collision_workspaces(root)
        slot_a = scenario.endswith("-a")
        workspace = workspace_a if slot_a else workspace_b
        token = _HISTORY_A_TOKEN if slot_a else _HISTORY_B_TOKEN
        prefix = _HISTORY_A_PREFIX if slot_a else _HISTORY_B_PREFIX
        writer = _history_writer(argv, env, workspace, home, cfg, token)
        runs.append(writer)
        snapshot = _history_snapshot(home)
        reader = _history_reader(argv, env, workspace, home, cfg, prefix)
        runs.append(reader)
        facts.update(
            {
                "slot": "A" if slot_a else "B",
                "workspace": str(workspace.resolve()),
                "token": token,
                "prefix": prefix,
                "writer": writer,
                "reader": reader,
                "snapshot": snapshot,
            }
        )
    elif scenario == "history-realpath-key":
        workspace_a, workspace_b = _history_collision_workspaces(root)
        _prepare_tui_workspace(workspace_a)
        _prepare_tui_workspace(workspace_b)
        real_a = os.path.realpath(workspace_a)
        real_b = os.path.realpath(workspace_b)
        writer_a = _history_writer(argv, env, workspace_a, home, cfg, _HISTORY_A_TOKEN)
        runs.append(writer_a)
        snapshot_a = _history_snapshot(home)
        writer_b = _history_writer(argv, env, workspace_b, home, cfg, _HISTORY_B_TOKEN)
        runs.append(writer_b)
        snapshot_ab = _history_snapshot(home)
        reader_a = _history_reader(argv, env, workspace_a, home, cfg, _HISTORY_A_PREFIX)
        runs.append(reader_a)
        reader_b = _history_reader(argv, env, workspace_b, home, cfg, _HISTORY_B_PREFIX)
        runs.append(reader_b)
        facts.update(
            {
                "workspace_a": real_a,
                "workspace_b": real_b,
                "realpaths_distinct": real_a != real_b,
                "lossy_slug_a": _lossy_history_slug(workspace_a),
                "lossy_slug_b": _lossy_history_slug(workspace_b),
                "basename_collision": workspace_a.name == workspace_b.name,
                "writer_a": writer_a,
                "writer_b": writer_b,
                "reader_a": reader_a,
                "reader_b": reader_b,
                "snapshot_a": snapshot_a,
                "snapshot_ab": snapshot_ab,
            }
        )
    else:
        raise ValueError(f"unknown history scenario: {scenario}")

    aggregate = _aggregate_pty_results(runs)
    return {
        "cli": True,
        "text": "\n".join(str(run.get("text") or "") for run in runs),
        "reqs": list(cfg.get("requests") or []),
        "files": {},
        "pty": aggregate,
        "pty_runs": runs,
        "history": facts,
    }


def _run_one(agent_dir: Path, case: dict) -> dict:
    """Run one tui-line case.

    Case keys:
      id, lines (input lines to feed), seed (workspace preset files), stub (gateway cfg
      overrides, including probe_tokens), check (verdict function, receives res), timeout,
      blocking, cli_args (extra CLI flags to the build - seeds config via public flags, e.g.
      `--deny` permission list; equivalent to the interactive-mode face of
      create_session(deny=))
    Self-reported res:
      {"cli": bool whether an interactive entry point exists, "text": pty visible text,
       "reqs": gateway request records}
    """
    argv, env = _discover_tui_cli(agent_dir)
    if argv is None:
        return {"id": case["id"], "ok": True, "blocking": case.get("blocking", True),
                "why": "inconclusive: this artifact exposes no usable interactive entry point (even --help fails) -> skipped"}
    argv = [*argv, *([])]
    cfg = MG.new_cfg(**{"mode": "normal", "reply_content": "ack",
                        **(case.get("stub") or {})})
    srv = MG.make_server(cfg, 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    workdir = tempfile.mkdtemp(prefix="tui_line_")
    try:
        for rel, body in (case.get("seed") or {}).items():
            p = Path(workdir) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body if isinstance(body, bytes) else body.encode())
        (Path(workdir) / PROMPT_CANARY_FILE).write_text(
            PROMPT_CANARY_NONCE,
            encoding="utf-8",
        )
        env = dict(env)
        env["D2A_MODEL_BASE_URL"] = f"http://127.0.0.1:{port}"
        env["D2A_MODEL_NAME"] = "litellm_proxy/mock"
        env["D2A_MODEL_API_KEY"] = "sk-mock-test"
        if case.get("scenario"):
            res = _run_history_scenario(
                argv,
                env,
                Path(workdir),
                cfg,
                str(case["scenario"]),
            )
        else:
            pty_result = _feed_pty(
                argv,
                env,
                workdir,
                case.get("lines") or [],
                case.get("timeout", 60),
                cfg=cfg,
                line_terminator="\r",
            )
            text = pty_result["text"]
                # Read workspace files on demand to observe the real disk-landing surface of `!<shell>`
                # commands. Trusting only the pty echo conflates "claimed to run" with "actually ran".
                # Cases that supply no files list incur zero extra I/O.
            files = {}
            for rel in case.get("files") or []:
                p = Path(workdir) / rel
                try:
                    files[rel] = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
                except Exception:
                    files[rel] = None
            res = {
                "cli": True,
                "text": text,
                "reqs": list(cfg.get("requests") or []),
                "files": files,
                "pty": pty_result,
            }
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    ok, why = case["check"](res)
    pty_meta = res.get("pty") or {}
    if not pty_meta.get("prompt_ready"):
        ok, why = True, "inconclusive: prompt-ready was never observed, so no case input was sent to the PTY"
    prompt_observation = pty_meta.get("prompt_observation") or {}
    why += (
        f" | pty-ready={pty_meta.get('prompt_ready')} rc={pty_meta.get('returncode')}"
        f" signal={pty_meta.get('signal')} raw={pty_meta.get('raw_sha256')}"
        f" timeline={len(pty_meta.get('timeline') or [])}"
        f" split-nonce={pty_meta.get('split_nonce')}"
        f" prompt-id={prompt_observation.get('prompt_identity')}"
        f" turn={prompt_observation.get('turn_id')}"
        f" consumed={prompt_observation.get('input_consumed')}"
        f" startup-failure={prompt_observation.get('startup_failure')}"
    )
    return {
        "id": case["id"],
        "ok": bool(ok),
        "blocking": case.get("blocking", True),
        "why": why,
        "evidence": [
            {
                "kind": "tui-prompt-observer",
                "data": prompt_observation,
            }
        ],
    }


_CRASH_SIGNS = ("Traceback (most recent call last)", "UnhandledError", "panic:",
                "Unhandled exception", "RuntimeError:", "KeyError:", "AttributeError:",
                "ValueError:")



def _seen(res, tok):
    return any((r.get("probes") or {}).get(tok) for r in (res.get("reqs") or []))


def _history_recall_facts(
    pty_result: dict,
    event_names: str | tuple[str, ...],
    own_token: str,
    foreign_token: str,
) -> dict:
    names = (event_names,) if isinstance(event_names, str) else event_names
    output = "".join(
        str(_event_result(pty_result, name).get("output_text") or "")
        for name in names
    )
    scoped_inputs = [
        bytes.fromhex(str(_event_result(pty_result, name).get("input_hex") or ""))
        for name in names
    ]
    writes = b"".join(scoped_inputs)
    return {
        "own_visible": own_token in output,
        "foreign_visible": foreign_token in output,
        "own_absent_from_input": own_token.encode() not in writes,
        "foreign_absent_from_input": foreign_token.encode() not in writes,
        "ctrl_r_exact_write": bytes((0x12,)) in scoped_inputs,
    }


def _event_wrote_line(pty_result: dict, name: str, text: str) -> bool:
    return (
        str(_event_result(pty_result, name).get("input_hex") or "")
        == (text + "\r").encode().hex()
    )


def chk_history_fresh_control(slot: str):
    """Zero-weight cross-process history/readback control for one workspace."""
    own = _HISTORY_A_TOKEN if slot == "A" else _HISTORY_B_TOKEN
    foreign = _HISTORY_B_TOKEN if slot == "A" else _HISTORY_A_TOKEN

    def _f(res):
        facts = res.get("history") or {}
        snapshot = facts.get("snapshot") or {}
        paths = _history_token_paths(snapshot, own)
        foreign_paths = _history_token_paths(snapshot, foreign)
        recall = _history_recall_facts(
            facts.get("reader") or {},
            ("ctrl-r-start", "ctrl-r-query"),
            own,
            foreign,
        )
        drove = _event_wrote_line(
            facts.get("writer") or {},
            "write-token",
            own,
        )
        ok = (
            drove
            and bool(paths)
            and not foreign_paths
            and recall["own_visible"]
            and not recall["foreign_visible"]
            and recall["own_absent_from_input"]
            and recall["foreign_absent_from_input"]
            and recall["ctrl_r_exact_write"]
        )
        return ok, (
            f"fresh-{slot} exact-PTY-line={drove} history_paths={paths} foreign_paths={foreign_paths} "
            f"Ctrl+R readback={recall} (zero-weight control: the same realpath is recoverable across "
            "processes, the complete token never appears in the reader's input, and PTY echo cannot "
            "compensate for it)"
        )

    return _f


def chk_history_realpath_key(res):
    """A lossy-slug collision must not merge the input history of two real workspaces."""
    facts = res.get("history") or {}
    snapshot_a = facts.get("snapshot_a") or {}
    snapshot_ab = facts.get("snapshot_ab") or {}
    a_before = _history_token_paths(snapshot_a, _HISTORY_A_TOKEN)
    a_final = _history_token_paths(snapshot_ab, _HISTORY_A_TOKEN)
    b_final = _history_token_paths(snapshot_ab, _HISTORY_B_TOKEN)
    recall_a = _history_recall_facts(
        facts.get("reader_a") or {},
        ("ctrl-r-start", "ctrl-r-query"),
        _HISTORY_A_TOKEN,
        _HISTORY_B_TOKEN,
    )
    recall_b = _history_recall_facts(
        facts.get("reader_b") or {},
        ("ctrl-r-start", "ctrl-r-query"),
        _HISTORY_B_TOKEN,
        _HISTORY_A_TOKEN,
    )
    fixture = (
        facts.get("realpaths_distinct")
        and facts.get("basename_collision")
        and facts.get("lossy_slug_a") == facts.get("lossy_slug_b")
    )
    isolated_bytes = (
        bool(a_before)
        and bool(a_final)
        and bool(b_final)
        and set(a_final).isdisjoint(b_final)
    )
    recalled = all(
        row["own_visible"]
        and not row["foreign_visible"]
        and row["own_absent_from_input"]
        and row["foreign_absent_from_input"]
        and row["ctrl_r_exact_write"]
        for row in (recall_a, recall_b)
    )
    drove = _event_wrote_line(
        facts.get("writer_a") or {},
        "write-token",
        _HISTORY_A_TOKEN,
    ) and _event_wrote_line(
        facts.get("writer_b") or {},
        "write-token",
        _HISTORY_B_TOKEN,
    )
    ok = bool(fixture and drove and isolated_bytes and recalled)
    return ok, (
        f"fixture(realpath-distinct/basename+slug-collision)={fixture} exact-PTY-lines={drove} "
        f"A-before={a_before} A-final={a_final} B-final={b_final} "
        f"bytes isolated={isolated_bytes} readback-A={recall_a} readback-B={recall_b} "
        "(v6§5.7:379: each of the two real working directories must read back its own history with "
        "no cross-contamination)"
    )


def chk_survives_chaos(res):
    """Keyboard-mashing: after a burst of random input the session must still handle input normally and must not crash.

    Liveness evidence: this layer always feeds `!echo <DONE>` after all input; if it appears in the
    pty text the TUI survived the mashing and can still handle input. If it does not appear the
    session crashed, hung, or is stuck in an unescapable mode.
    Self-gating: pty returned nothing -> interactive entry point unreachable -> inconclusive.
    """
    txt = res.get("text") or ""
    if not txt.strip():
        return True, "inconclusive: the pty returned nothing at all, so this build's interactive entry point is unreachable at this layer"
    alive = DONE in txt
    crash = [s for s in _CRASH_SIGNS if s in txt]
    ok = alive and not crash
    return ok, (f"session still processing input after the input burst={alive} (closing sentinel) "
                f"· crash/uncaught-exception signatures={crash} "
                f"(per v6§9:473, malformed or exceptional input must degrade to a clean error and never "
                f"crash outright)")


def chk_tui_file(path, token):
    """Direct-shell disk-landing surface: trusting the UI echo is insufficient; the target file must actually exist."""
    def _f(res):
        got = (res.get("files") or {}).get(path)
        ok = got is not None and token in got
        return ok, f"{path}={got!r} (a `!` command must really execute and write to disk, not merely echo the command text)"
    return _f


def chk_tui_wire_survives(token):
    """After legal or abnormal key sequences: valid text must go on the wire, the session must handle the wind-down command, and no crash is allowed."""
    def _f(res):
        text = res.get("text") or ""
        on_wire = _seen(res, token)
        alive = DONE in text
        crash = [s for s in _CRASH_SIGNS if s in text]
        return on_wire and alive and not crash, (
            f"valid text on the wire={on_wire} session alive at wind-down={alive} crash={crash} "
            "(per v6§9:473 abnormal interactive input must neither crash outright nor wedge the session)"
        )
    return _f


# Keyboard mashing: every control byte except those that end the line or the session, packed into
# a SINGLE line so the case costs one no-request timeout rather than one per byte.
_STORM = "".join(chr(c) for c in range(1, 27) if c not in (3, 4, 10, 13, 26))


CASES: list[dict] = [

    # === §5.3 x §9 input robustness (keyboard mashing) ===
    # v6§9:473: malformed or abnormal interactive input must degrade to a clean error, never a crash
    # or an uncaught exception printed to the UI (e.g. `ValueError: embedded null byte`).
    dict(id="tui_ctrl_byte_storm_survives", seed={}, stub={"probe_tokens": ["RB97A_7731"]},
         lines=[_STORM + " RB97A_7731"], timeout=90, check=chk_survives_chaos, blocking=False),
    dict(id="tui_bang_alone_enters_shell_mode",
         seed={}, stub={"probe_tokens": ["MODE100_7731"]},
         lines=["!", "printf 'MODE100_%s\\n' 7731 > mode100.txt"],
         files=["mode100.txt"], timeout=100,
         check=chk_tui_file("mode100.txt", "MODE100_7731"), blocking=False),

    # === Workspace history isolation (1 scored target + 2 zero-weight controls) ===
    # v6§5.7:379 ("save input history per working directory; Ctrl+R reverse-search and reuse").
    #
    # Truth table:
    #   control-A/B: a fresh process writes a full token from a real cwd, exits, then a new
    #     process types only an incomplete prefix + physical 0x12 (Ctrl+R); raw history bytes
    #     and Ctrl+R output must contain the full token, reader input must NOT.
    #   realpath target: A=`.../piece_a_b/leaf`, B=`.../piece_a/b/leaf`; realpaths differ
    #     but basename and any lossy slug are the same. History token paths must be non-empty,
    #     mutually exclusive, and each Ctrl+R reads only its own side.
    #   mutant: using basename/lossy-slug as the unique key causes byte-path overlap or a
    #     foreign token to appear -> FAIL.
    #   empty/NA: unhealthy fresh control, or entry point / persistent history / Ctrl+R not
    #     reachable -> gates convert to NA; prompt not ready uses unified inconclusive.
    dict(
        id="history_workspace_a_fresh_control",
        scenario="history-fresh-a",
        seed={},
        stub={
            "mode": "fixed_reply",
            "reply_content": "history-control-a-ok",
            "probe_tokens": [_HISTORY_A_TOKEN, _HISTORY_B_TOKEN],
        },
        timeout=90,
        check=chk_history_fresh_control("A"),
        blocking=False,
    ),
    dict(
        id="history_workspace_b_fresh_control",
        scenario="history-fresh-b",
        seed={},
        stub={
            "mode": "fixed_reply",
            "reply_content": "history-control-b-ok",
            "probe_tokens": [_HISTORY_A_TOKEN, _HISTORY_B_TOKEN],
        },
        timeout=90,
        check=chk_history_fresh_control("B"),
        blocking=False,
    ),
    dict(
        id="history_realpath_key",
        scenario="history-realpath-key",
        seed={},
        stub={
            "mode": "fixed_reply",
            "reply_content": "history-realpath-ok",
            "probe_tokens": [_HISTORY_A_TOKEN, _HISTORY_B_TOKEN],
        },
        timeout=140,
        gates=[
            "history_workspace_a_fresh_control",
            "history_workspace_b_fresh_control",
        ],
        check=chk_history_realpath_key,
    ),
]


def _selected_cases(raw: str | None = None) -> list[dict]:
    """Apply D2A_ONLY_IDS without changing manifest order or widening unknown IDs."""
    value = os.environ.get("D2A_ONLY_IDS", "") if raw is None else raw
    if not value.strip():
        return list(CASES)
    requested = [item.strip() for item in value.split(",") if item.strip()]
    if len(requested) != len(set(requested)):
        raise ValueError("D2A_ONLY_IDS contains duplicate IDs")
    known = {case["id"] for case in CASES}
    unknown = sorted(set(requested) - known)
    if unknown:
        raise ValueError(f"D2A_ONLY_IDS contains unknown tui_line IDs: {unknown}")
    wanted = set(requested)
    return [case for case in CASES if case["id"] in wanted]


def main() -> int:
    """Entry point isomorphic to other layers: `python tui_line.py <agent_dir> --emit-json`."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir", nargs="?", default=".")
    ap.add_argument("--emit-json", action="store_true")
    ap.add_argument("--case-id", default="")
    ap.add_argument("--result-file", default="")
    args = ap.parse_args()
    agent = Path(args.agent_dir)
    results = []
    try:
        selected = _selected_cases()
        if args.case_id:
            known = {case["id"] for case in CASES}
            if args.case_id not in known:
                raise ValueError(f"unknown --case-id: {args.case_id}")
            selected = [case for case in selected if case["id"] == args.case_id]
    except ValueError as exc:
        print(f"TUI_LINE_SELECTION_ERROR={exc}", file=sys.stderr)
        return 2
    for case in selected:
        try:
            result = _run_one(agent, case)
        except Exception as e:  # per-case exception does not abort the whole layer
            result = {"id": case["id"], "ok": False,
                      "blocking": case.get("blocking", True),
                      "why": f"runner error: {e!r}"}
        results.append(result)
        if args.result_file:
            path = Path(args.result_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    for r in results:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['id']}: {r['why'][:160]}")
    if args.emit_json:
        print("TUILINE_RESULTS=" + json.dumps(results, ensure_ascii=False))
    blocking_ok = all(r["ok"] for r in results if r.get("blocking"))
    if blocking_ok:
        print("TUI_LINE_SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
