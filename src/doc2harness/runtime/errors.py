"""Stable, path-free failures for the local runtime boundary."""

from __future__ import annotations


class RuntimeBoundaryError(RuntimeError):
    """A closed runtime failure which never embeds host paths or raw output."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if message is None:
            message = code
            code = "SBH-RUNTIME-ERROR"
        if type(code) is not str or type(message) is not str:
            raise TypeError("runtime errors require exact strings")
        super().__init__(message)
        self.code = code


class RuntimeCapabilityUnavailable(RuntimeBoundaryError):
    """A required platform primitive cannot be proved by the injected backend."""


__all__ = ["RuntimeBoundaryError", "RuntimeCapabilityUnavailable"]
