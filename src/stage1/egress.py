#!/usr/bin/env python3
"""Egress allowlist for the build container.

Threat model: the builder has network access during a build, so it could `curl` the
public internet for outside help or for the evaluation scenarios, which are held out
from the build. This module confines the fresh build container's egress to the
local gateway only (the sole legitimate model exit) and drops everything else.

Implementation is an in-container iptables allowlist: OUTPUT defaults to DROP;
allow loopback, ESTABLISHED/RELATED, Docker's embedded DNS (127.0.0.11), and the
gateway IP:port only. Requires iptables in the image (see Dockerfile) and
`docker run --cap-add=NET_ADMIN` (see run_extra_args).

Degrades gracefully: if iptables or the capability is missing we warn but do not
abort, so an already-working chain keeps running. For a comparable run, make sure
the prerequisites hold and check verify().

Used by run_build.py when `--egress gateway-only` is set.
"""
from __future__ import annotations


def run_extra_args() -> list[str]:
    """Extra `docker run` args needed for gateway-only egress (iptables needs NET_ADMIN)."""
    return ["--cap-add=NET_ADMIN"]


async def harden(run, docker: list[str], name: str, gateway_port: int,
                 gateway_host: str = "host.docker.internal") -> dict:
    """Apply the egress allowlist to the already-started container `name`.
    `run` is injector._run (async, returns (rc, out, err)).

    Returns {applied: bool, gateway_ip, reason}. Only warns on failure, never
    raises -- the caller decides whether to record or surface it."""
    info: dict = {"applied": False, "gateway_ip": "", "reason": ""}

    # 0) Prerequisite: is iptables present in the container?
    rc, _, _ = await run(*docker, "exec", name, "bash", "-lc", "command -v iptables", timeout=30)
    if rc != 0:
        info["reason"] = ("iptables not found in the image; egress is unrestricted. "
                          "Rebuild the builder image with iptables installed.")
        return info

    # 1) Resolve the gateway IP (--add-host writes host.docker.internal into /etc/hosts)
    rc, out, _ = await run(*docker, "exec", name, "getent", "hosts", gateway_host, timeout=30)
    gw_ip = out.split()[0].strip() if (rc == 0 and out.split()) else ""
    if not gw_ip:
        info["reason"] = (f"could not resolve {gateway_host}; egress is unrestricted. "
                          f"Check that docker run passes --add-host.")
        return info
    info["gateway_ip"] = gw_ip

    # 2) Install the allowlist (one atomic bash -lc; failures usually mean no NET_ADMIN)
    rules = "; ".join([
        "iptables -F OUTPUT",
        "iptables -A OUTPUT -o lo -j ACCEPT",
        "iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT",
        "iptables -A OUTPUT -d 127.0.0.11 -j ACCEPT",            # Docker embedded DNS
        f"iptables -A OUTPUT -d {gw_ip} -p tcp --dport {gateway_port} -j ACCEPT",  # gateway only
        "iptables -P OUTPUT DROP",                                # drop all other egress
    ])
    rc, _, err = await run(*docker, "exec", name, "bash", "-lc", rules, timeout=60)
    if rc != 0:
        info["reason"] = (f"failed to install iptables rules, usually a missing "
                          f"--cap-add=NET_ADMIN: {err[:200]}")
        return info
    info["applied"] = True
    info["reason"] = f"egress restricted to {gw_ip}:{gateway_port}; all other traffic dropped"
    return info


async def verify(run, docker: list[str], name: str, blocked_url: str = "https://github.com") -> dict:
    """Self-check: once the allowlist is active, reaching the internet must fail.
    Returns {blocked: bool, detail}. Costs no model tokens (a single curl that
    times out or is refused)."""
    rc, _, _ = await run(*docker, "exec", name, "bash", "-lc",
                         f"curl -sS -m 6 -o /dev/null {blocked_url}", timeout=20)
    return {"blocked": rc != 0, "probe_url": blocked_url,
            "detail": ("internet unreachable, allowlist is in effect" if rc != 0
                       else "internet still reachable, allowlist is NOT in effect")}
