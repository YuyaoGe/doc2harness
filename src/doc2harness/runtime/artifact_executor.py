"""Fail-closed execution boundary for reviewed production artifact drivers.

The executor owns only the process boundary.  A harness-specific driver owns
fixture materialisation and recorder hooks; it must publish typed facts through
the declared side channel.  stdout, stderr, and trajectory text are
diagnostics and are never promoted to benchmark evidence.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from doc2harness.contracts.primitives import ContractDocumentError
from doc2harness.runtime.artifact_store import canonical_artifact_digest
from doc2harness.runtime.errors import RuntimeBoundaryError

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_HOOK_RELATIVE = Path(".d2a_spec") / "typed_event_hooks.json"


class ArtifactExecutionError(RuntimeBoundaryError):
    """A stable, path-free failure from the production artifact boundary."""


@dataclass(frozen=True, slots=True)
class ArtifactExecutionRequest:
    """Inputs for one driver invocation against an immutable source artifact."""

    artifact: Path
    driver_root: Path
    driver_argv: tuple[str, ...]
    output: Path
    source_digest: str
    task_digest: str
    budget_digest: str
    artifact_digest: str
    timeout_seconds: float = 900.0

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, Path) or not isinstance(self.driver_root, Path):
            raise ContractDocumentError("artifact execution paths must use Path")
        if not isinstance(self.output, Path):
            raise ContractDocumentError("artifact execution output must use Path")
        if type(self.driver_argv) is not tuple or not self.driver_argv:
            raise ContractDocumentError("artifact driver argv must be non-empty")
        if any(type(item) is not str or not item or "\0" in item for item in self.driver_argv):
            raise ContractDocumentError("artifact driver argv contains an invalid item")
        for value, name in (
            (self.source_digest, "source digest"),
            (self.task_digest, "task digest"),
            (self.budget_digest, "budget digest"),
            (self.artifact_digest, "artifact digest"),
        ):
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ContractDocumentError(f"{name} is invalid")
        if type(self.timeout_seconds) not in {int, float} or not 0 < self.timeout_seconds <= 3600:
            raise ContractDocumentError("artifact execution timeout is invalid")


def artifact_tree_digest(root: Path, /) -> str:
    """Hash a source tree with the verified artifact-store algorithm."""

    if not isinstance(root, Path) or not root.is_dir() or not (root / "run_agent.sh").is_file():
        raise ArtifactExecutionError("SBH-ARTIFACT-UNAVAILABLE", "artifact is unavailable")
    try:
        return canonical_artifact_digest(root)
    except (OSError, UnicodeError, ValueError, RuntimeBoundaryError):
        raise ArtifactExecutionError("SBH-ARTIFACT-SCAN", "artifact tree cannot be scanned") from None


def require_typed_event_hook_abi(root: Path, /) -> dict[str, object]:
    """Require the reviewed side-channel declaration on the driver package."""

    marker = root / _HOOK_RELATIVE
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ArtifactExecutionError(
            "SBH-TYPED-HOOK-ABI", "typed event hook ABI is missing or invalid"
        ) from None
    required = {"schema", "tool_lifecycle", "subprocess", "workspace", "cleanup"}
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("schema") != "doc2harness.typed-event-hooks/1"
        or any(value.get(name) is not True for name in required - {"schema"})
    ):
        raise ArtifactExecutionError("SBH-TYPED-HOOK-ABI", "typed event hook ABI is incomplete")
    return value


def _stderr_hint(stderr: bytes | str | None, scratch: str) -> str:
    """The driver's final stderr line, with host paths removed.

    Closed errors must not leak filesystem layout, so the private scratch root
    is replaced and any remaining absolute path is reduced to its basename.
    What survives -- an exception type, a missing key, a docker refusal -- is
    the part that says which failure this was.
    """

    if not stderr:
        return ""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1].replace(scratch, "<scratch>")
    last = re.sub(r"(?<![\w/])/[\w./+-]{3,}", lambda m: m.group(0).rsplit("/", 1)[-1], last)
    return f": {last[:300]}"


def load_typed_event_document(
    path: Path,
    *,
    source_digest: str,
    task_digest: str,
    budget_digest: str,
    artifact_digest: str,
) -> dict[str, object]:
    """Load and bind one driver-produced typed-event envelope."""

    expected = {
        "source_digest": source_digest,
        "task_digest": task_digest,
        "budget_digest": budget_digest,
        "artifact_digest": artifact_digest,
    }
    if any(_DIGEST.fullmatch(value) is None for value in expected.values()):
        raise ContractDocumentError("typed event invocation binding is invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ArtifactExecutionError("SBH-TYPED-EVENTS", "typed event output is unreadable") from None
    if not isinstance(value, dict) or value.get("schema") != "doc2harness.typed-events/1":
        raise ArtifactExecutionError("SBH-TYPED-EVENTS", "typed event schema is unsupported")
    for name, digest in expected.items():
        if value.get(name) != digest:
            raise ArtifactExecutionError("SBH-TYPED-EVENTS", "typed event binding differs from invocation")
    events = value.get("events")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise ArtifactExecutionError("SBH-TYPED-EVENTS", "typed event list is invalid")
    return value


def run_artifact_driver(
    request: ArtifactExecutionRequest,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Run a hook-capable driver against a private artifact copy.

    The source artifact is never edited.  The driver receives only explicit
    digest bindings and a path to the private copy; its typed side channel is
    the sole accepted source of observations.
    """

    source_digest = artifact_tree_digest(request.artifact)
    if source_digest != request.artifact_digest:
        raise ArtifactExecutionError("SBH-ARTIFACT-IDENTITY", "artifact identity differs from request")
    require_typed_event_hook_abi(request.driver_root)
    request.output.parent.mkdir(parents=True, exist_ok=True)
    child_env = dict(os.environ)
    if environ is not None:
        child_env.update(environ)
    with tempfile.TemporaryDirectory(prefix="sbh-artifact-") as temporary:
        private_artifact = Path(temporary) / "artifact"
        typed_output = Path(temporary) / "typed_events.json"
        shutil.copytree(request.artifact, private_artifact)
        child_env.update(
            {
                "SBH_ARTIFACT_PATH": str(private_artifact),
                "SBH_TYPED_EVENT_PATH": str(typed_output),
                "SBH_SOURCE_DIGEST": request.source_digest,
                "SBH_TASK_DIGEST": request.task_digest,
                "SBH_BUDGET_DIGEST": request.budget_digest,
                "SBH_ARTIFACT_DIGEST": request.artifact_digest,
            }
        )
        try:
            completed = subprocess.run(
                request.driver_argv,
                cwd=private_artifact,
                env=child_env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=request.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ArtifactExecutionError("SBH-ARTIFACT-TIMEOUT", "artifact driver timed out") from None
        except OSError:
            raise ArtifactExecutionError("SBH-ARTIFACT-DRIVER", "artifact driver could not start") from None
        if completed.returncode != 0:
            # Carry the driver's own last words.  Without them every driver
            # crash -- a missing binary, a scenario bug, a transient docker
            # refusal -- arrives as the same five words, and the only way to
            # learn which one happened is to re-run the driver by hand outside
            # the executor, rebuilding this environment from memory.  The
            # closed-error rule bars host paths and raw output, so the tail is
            # scrubbed of both before it is attached.
            raise ArtifactExecutionError(
                "SBH-ARTIFACT-DRIVER",
                f"artifact driver failed (rc={completed.returncode})"
                f"{_stderr_hint(completed.stderr, temporary)}",
            )
        if not typed_output.is_file():
            raise ArtifactExecutionError("SBH-TYPED-EVENTS", "artifact driver produced no typed event output")
        load_typed_event_document(
            typed_output,
            source_digest=request.source_digest,
            task_digest=request.task_digest,
            budget_digest=request.budget_digest,
            artifact_digest=request.artifact_digest,
        )
        request.output.write_bytes(typed_output.read_bytes())
    return request.output


__all__ = [
    "ArtifactExecutionError",
    "ArtifactExecutionRequest",
    "artifact_tree_digest",
    "load_typed_event_document",
    "require_typed_event_hook_abi",
    "run_artifact_driver",
]
