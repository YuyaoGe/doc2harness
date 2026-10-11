"""The fixed entry point through which external programs drive this agent.

For the contract, see `.d2a_spec/functional_spec.md` §0.1.2.

These signatures must not change: external programs call exactly this shape.
"""
from __future__ import annotations


def create_session(*, deny=None, mode=None, role=None, cwd=None) -> "AgentSession":
    raise NotImplementedError


class AgentSession:
    @property
    def cwd(self) -> str:
        raise NotImplementedError

    @property
    def messages(self) -> list:
        raise NotImplementedError

    def dispatch(self, command: str) -> str:
        raise NotImplementedError

    def chat(self, text: str) -> str:
        raise NotImplementedError

    def config_get(self, key: str):
        raise NotImplementedError

    @property
    def mcp_servers(self) -> list:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
