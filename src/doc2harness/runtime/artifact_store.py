"""Trusted local artifact materialization for read-only Harness consumers.

The runner-owned locator supplies only a host location.  This store
independently recomputes the directory digest before transferring the same
pinned descriptor to the reader.  Python object privacy is not treated as a security
boundary and no caller-created receipt can authorize bytes.
"""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from dataclasses import field as _field
from doc2harness.runtime.errors import (
    RuntimeBoundaryError,
    RuntimeCapabilityUnavailable,
)

_IGNORED_COMPONENTS = frozenset(
    {
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
)
_MAX_DEPTH = 128
_MAX_NAME_BYTES = 255
_MAX_ROOT_BYTES = 4096
_MAX_TREE_BYTES = 512 * 1024 * 1024
_MAX_MEMBERS = 1_000_000
_READ_CHUNK = 64 * 1024


def _fail(code: str, message: str) -> RuntimeBoundaryError:
    return RuntimeBoundaryError(code, message)


def _safe_component(value: str) -> bool:
    if type(value) is not str or value in {"", ".", ".."}:
        return False
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return (
        len(encoded) <= _MAX_NAME_BYTES
        and unicodedata.normalize("NFC", value) == value
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _directory_flags() -> int:
    names = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in names):
        raise RuntimeCapabilityUnavailable(
            "SBH-ARTIFACT-UNSUPPORTED",
            "safe artifact directory access is unavailable",
        )
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_CLOEXEC"):
        raise RuntimeCapabilityUnavailable(
            "SBH-ARTIFACT-UNSUPPORTED",
            "safe artifact file access is unavailable",
        )
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _binding(details: os.stat_result) -> tuple[int, int, int]:
    return (details.st_dev, details.st_ino, stat.S_IFMT(details.st_mode))


def _close_descriptors(*descriptors: int) -> None:
    """Attempt every owned close and expose only a path-free typed failure."""

    failed = False
    for descriptor in descriptors:
        if descriptor < 0:
            continue
        try:
            os.close(descriptor)
        except OSError:
            failed = True
    if failed:
        raise _fail("SBH-ARTIFACT-INVARIANT", "artifact descriptors did not close safely")


def _open_root(
    root: str | bytes,
) -> tuple[int, int, tuple[str, ...], tuple[tuple[int, int], ...]]:
    if type(root) not in {str, bytes}:
        raise _fail("SBH-ARTIFACT-ROOT", "artifact root is invalid")
    size = len(root) if type(root) is bytes else len(root.encode("utf-8", "surrogatepass"))
    decoded = os.fsdecode(root)
    if (
        not root
        or size > _MAX_ROOT_BYTES
        or not decoded.startswith(os.sep)
        or decoded.startswith("//")
        or decoded.endswith(os.sep)
        or os.path.normpath(decoded) != decoded
    ):
        raise _fail("SBH-ARTIFACT-ROOT", "artifact root is invalid")
    parts = tuple(decoded.split(os.sep)[1:])
    if not parts or len(parts) > _MAX_DEPTH or any(not _safe_component(part) for part in parts):
        raise _fail("SBH-ARTIFACT-ROOT", "artifact root is invalid")
    anchor = current = -1
    identities: list[tuple[int, int]] = []
    try:
        anchor = os.open(os.sep, _directory_flags())
        current = os.dup(anchor)
        for component in parts:
            replacement = os.open(component, _directory_flags(), dir_fd=current)
            previous = current
            current = replacement
            _close_descriptors(previous)
            details = os.fstat(current)
            if not stat.S_ISDIR(details.st_mode):
                raise OSError("artifact root component is not a directory")
            identities.append((details.st_dev, details.st_ino))
        return current, anchor, parts, tuple(identities)
    except (RuntimeBoundaryError, RuntimeCapabilityUnavailable):
        _close_descriptors(current, anchor)
        raise
    except OSError:
        _close_descriptors(current, anchor)
        raise _fail("SBH-ARTIFACT-ROOT", "artifact root cannot be opened safely") from None


def _read_regular(parent_fd: int, name: str, before: os.stat_result, remaining: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if (
            _binding(before) != _binding(opened)
            or before.st_nlink != 1
            or opened.st_nlink != 1
            or before.st_size != opened.st_size
            or opened.st_size > remaining
        ):
            raise _fail("SBH-ARTIFACT-UNSAFE", "artifact file identity or size is unsafe")
        chunks: list[bytes] = []
        left = opened.st_size
        while left:
            chunk = os.read(descriptor, min(_READ_CHUNK, left))
            if not chunk:
                raise _fail("SBH-ARTIFACT-UNSAFE", "artifact file changed during verification")
            chunks.append(chunk)
            left -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)
        terminal = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _binding(opened) != _binding(after)
            or _binding(after) != _binding(terminal)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            raise _fail("SBH-ARTIFACT-UNSAFE", "artifact file changed during verification")
        return content
    except RuntimeBoundaryError:
        raise
    except OSError:
        raise _fail("SBH-ARTIFACT-UNSAFE", "artifact file cannot be verified safely") from None
    finally:
        _close_descriptors(descriptor)


def _tree_digest(root_fd: int, *, max_tree_bytes: int, max_members: int) -> tuple[str, int, int]:
    files: list[tuple[str, str]] = []
    member_count = 0
    byte_count = 0

    def visit(directory_fd: int, prefix: tuple[str, ...], depth: int) -> None:
        nonlocal member_count, byte_count
        if depth > _MAX_DEPTH:
            raise _fail("SBH-ARTIFACT-OVERSIZE", "artifact tree exceeds the depth limit")
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError:
            raise _fail("SBH-ARTIFACT-UNSAFE", "artifact directory cannot be enumerated safely") from None
        for name in names:
            if not _safe_component(name):
                raise _fail("SBH-ARTIFACT-UNSAFE", "artifact member name is unsafe")
            member_count += 1
            if member_count > max_members:
                raise _fail("SBH-ARTIFACT-OVERSIZE", "artifact tree exceeds the member limit")
            if name in _IGNORED_COMPONENTS:
                continue
            try:
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise _fail("SBH-ARTIFACT-UNSAFE", "artifact member cannot be inspected") from None
            relative = prefix + (name,)
            if stat.S_ISLNK(before.st_mode):
                # Links are never traversal input.  A dangling link is inert,
                # so it is excluded from the digest; becoming resolvable at any
                # later verification is a tree mutation and fails closed.
                try:
                    os.stat(name, dir_fd=directory_fd, follow_symlinks=True)
                except FileNotFoundError:
                    continue
                except OSError:
                    raise _fail("SBH-ARTIFACT-UNSAFE", "artifact link cannot be classified safely") from None
                raise _fail("SBH-ARTIFACT-UNSAFE", "resolvable artifact links are forbidden")
            if stat.S_ISDIR(before.st_mode):
                child = -1
                try:
                    child = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    opened = os.fstat(child)
                    if _binding(before) != _binding(opened):
                        raise _fail("SBH-ARTIFACT-UNSAFE", "artifact directory identity changed")
                    visit(child, relative, depth + 1)
                    after = os.fstat(child)
                    terminal = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        _binding(opened) != _binding(after)
                        or _binding(after) != _binding(terminal)
                        or opened.st_mtime_ns != after.st_mtime_ns
                        or opened.st_ctime_ns != after.st_ctime_ns
                    ):
                        raise _fail("SBH-ARTIFACT-UNSAFE", "artifact directory changed during verification")
                except RuntimeBoundaryError:
                    raise
                except OSError:
                    raise _fail("SBH-ARTIFACT-UNSAFE", "artifact directory cannot be verified safely") from None
                finally:
                    _close_descriptors(child)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise _fail("SBH-ARTIFACT-UNSAFE", "artifact member type is unsafe")
            content = _read_regular(directory_fd, name, before, max_tree_bytes - byte_count)
            byte_count += len(content)
            files.append(("/".join(relative), hashlib.sha256(content).hexdigest()))

    root_before = os.fstat(root_fd)
    visit(root_fd, (), 0)
    root_after = os.fstat(root_fd)
    if (
        _binding(root_before) != _binding(root_after)
        or root_before.st_mtime_ns != root_after.st_mtime_ns
        or root_before.st_ctime_ns != root_after.st_ctime_ns
    ):
        raise _fail("SBH-ARTIFACT-UNSAFE", "artifact root changed during verification")
    digest = hashlib.sha256()
    for relative, file_digest in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(file_digest.encode("ascii"))
    return "sha256:" + digest.hexdigest(), member_count, byte_count


def canonical_artifact_digest(root: str | os.PathLike[str], /) -> str:
    """Compute the same digest used by the verified artifact store.

    Production execution must bind to the store's tree digest rather
    than to a second, weaker path walk.  The descriptor-relative walk also
    applies the store's ignored-component, symlink, and mutation checks.
    """

    root_fd, anchor_fd, _, _ = _open_root(os.fspath(root))
    try:
        digest, _, _ = _tree_digest(
            root_fd,
            max_tree_bytes=_MAX_TREE_BYTES,
            max_members=_MAX_MEMBERS,
        )
        return digest
    finally:
        _close_descriptors(root_fd, anchor_fd)


# Dataclass construction is complete; keep reflection evidence deterministic.
del _field


__all__ = [
    
]
