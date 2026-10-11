#!/usr/bin/env python3
"""Reference implementation for calling the model.

The endpoint is **OpenAI-compatible**: `POST {D2A_MODEL_BASE_URL}/chat/completions`.
Three environment variables supply the connection parameters:

    D2A_MODEL_BASE_URL   e.g. http://host.docker.internal:4000
    D2A_MODEL_NAME       e.g. litellm_proxy/builder
    D2A_MODEL_API_KEY    gateway key

Any client library may be used. The environment is offline and cannot install
new packages; `litellm`, `openai`, `httpx`, and `requests` are preinstalled, as
is the standard library's `urllib`.

To confirm connectivity with a single round trip:

    python .d2a_spec/model_call_reference.py
"""
from __future__ import annotations

import json
import os

# On import, litellm fetches its price table from the public internet, which
# hangs in an offline environment. Disable that first.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a bash command.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}


def _client():
    """An OpenAI-compatible client. The base URL must end in `/v1`, or requests 404."""
    from openai import OpenAI

    base = os.environ["D2A_MODEL_BASE_URL"].rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return OpenAI(base_url=base, api_key=os.environ["D2A_MODEL_API_KEY"])


def model_name() -> str:
    """Resolve `D2A_MODEL_NAME`, which takes the form `litellm_proxy/builder`.

    The `litellm_proxy/` prefix is a **routing prefix belonging to the litellm
    client**: pass it through verbatim when calling via litellm, but strip it for
    direct HTTP calls (the OpenAI client, httpx, requests). Leaving it in place
    makes the endpoint respond
    `400 Invalid model name passed in model=litellm_proxy/builder`.
    """
    name = os.environ["D2A_MODEL_NAME"]
    return name.split("/", 1)[1] if name.startswith("litellm_proxy/") else name


def complete(messages: list[dict], *, tools: list | None = None):
    """Perform one model call and return the raw response object."""
    kwargs = {"model": model_name(), "messages": messages}
    if tools:
        kwargs["tools"] = tools
    return _client().chat.completions.create(**kwargs)


def agentic_turn(messages: list[dict], run_bash, *, max_steps: int = 20) -> str:
    """Run one turn: expose the bash tool, execute the tool calls the model makes,
    and continue until the model replies with plain text.

    The caller supplies `run_bash(cmd: str) -> str`, which executes the command
    and returns its combined stdout and stderr. `messages` is appended to in
    place, with both assistant and `role=tool` messages.
    """
    for _ in range(max_steps):
        msg = complete(messages, tools=[BASH_TOOL]).choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            return msg.content or ""
        messages.append(msg.model_dump(exclude_none=True))
        for tc in calls:
            cmd = json.loads(tc.function.arguments or "{}").get("command", "")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": run_bash(cmd)})
    return ""


def usage_of(response) -> dict:
    """Token usage, or an empty dict when absent — cost accounting must tolerate
    the field being missing."""
    u = getattr(response, "usage", None)
    if u is None:
        return {}
    return u.model_dump() if hasattr(u, "model_dump") else dict(u)


if __name__ == "__main__":
    r = complete([{"role": "user", "content": "hi (one short sentence)"}])
    print("round-trip OK ·", repr((r.choices[0].message.content or "")[:80]))
    print("usage:", usage_of(r))
