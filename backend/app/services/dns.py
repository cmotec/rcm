"""Search-domain resolution without touching the Docker host's DNS configuration.

For a single-label hostname, each ``host.domain`` candidate is looked up with
the container's resolver (``getaddrinfo``, so /etc/hosts and Docker DNS
apply). The first candidate that resolves wins. If none resolve, the first
candidate is returned and the protocol layer reports the error. Qualified
names and IP literals are never modified.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

from app.services.resolution import hostname_candidates, is_qualified


@dataclass
class HostResolution:
    original: str
    effective: str
    candidates: list[str]
    resolved: bool
    note: str | None = None


async def _resolves(name: str, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    # A trailing dot stops the libc resolver from appending /etc/resolv.conf search domains.
    query = name if name.endswith(".") else name + "."
    try:
        await asyncio.wait_for(loop.getaddrinfo(query, None, type=socket.SOCK_STREAM), timeout)
        return True
    except (TimeoutError, OSError, UnicodeError):
        return False


async def apply_search_domains(host: str, domains: list[str], timeout: float = 2.0) -> HostResolution:
    host = host.strip()
    if is_qualified(host) or not domains:
        return HostResolution(host, host, [host], resolved=True, note="unchanged")
    candidates = hostname_candidates(host, domains)
    results = await asyncio.gather(*(_resolves(c, timeout) for c in candidates))
    for cand, ok in zip(candidates, results, strict=True):
        if ok:
            return HostResolution(host, cand, candidates, resolved=True)
    return HostResolution(host, candidates[0], candidates, resolved=False, note="no candidate resolved")
