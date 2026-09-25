"""The row of links to the owner's other apps, in the page header.

Each sibling app reports its own browser address as `browser_origin` on its
health route. This module asks each configured sibling on loopback, keeps
the answers for a minute, and hands the page `{name, origin, local}` per
sibling that answered: `origin` is where a browser elsewhere can open it
("" when it only answers on this machine), `local` is its loopback address.
The page picks one of the two from its own address (frontend/src/appLinks.js).

A sibling that doesn't answer inside the timeout is left out, so the row
never offers a link that won't open. The list of siblings is config
(`sibling_apps`), never discovered: the probe only talks to addresses on
this machine that the owner named.
"""

import asyncio
import time
from urllib.parse import urlsplit

import httpx

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
PROBE_TIMEOUT_S = 1.0
CACHE_TTL_S = 60.0
# A health answer is a few hundred bytes. Anything far bigger isn't one.
MAX_BODY_BYTES = 65536


def clean_origin(value) -> str:
    """`scheme://host[:port]` for an http or https origin, else "". A
    sibling's answer ends up in an href, so nothing else gets through."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        u = urlsplit(value.strip())
        u.port  # raises on a malformed port
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.hostname:
        return ""
    if u.username or u.password or u.query or u.fragment \
            or u.path not in ("", "/"):
        return ""
    return f"{u.scheme}://{u.netloc.lower()}"


def local_origin(url) -> str:
    """The origin of a URL on this machine, or "" for anywhere else."""
    try:
        u = urlsplit(str(url or "").strip())
        u.port
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or u.hostname not in LOOPBACK_HOSTS:
        return ""
    return f"{u.scheme}://{u.netloc.lower()}"


def own_origin(settings) -> str:
    """Where a browser can open this app: the explicit `browser_origin`, else
    https at the first trusted host (Tailscale serve at the root of the
    tailnet name), else loopback."""
    explicit = clean_origin(settings.browser_origin)
    if explicit:
        return explicit
    hosts = [h.strip().lower() for h in settings.trusted_hosts.split(",")
             if h.strip()]
    if hosts:
        return f"https://{hosts[0]}"
    return f"http://127.0.0.1:{settings.port}"


async def read_health(client: httpx.AsyncClient, url: str) -> dict | None:
    """One sibling's health answer, or None unless it is a 200 JSON object."""
    try:
        r = await client.get(url)
        if r.status_code != 200 or len(r.content) > MAX_BODY_BYTES:
            return None
        data = r.json()
    except Exception:
        return None
    return data if isinstance(data, dict) else None


class SiblingProbe:
    """The siblings that answered, asked at most once per `ttl` seconds."""

    def __init__(self, siblings: dict, *, timeout: float = PROBE_TIMEOUT_S,
                 ttl: float = CACHE_TTL_S, transport=None, clock=time.monotonic):
        # Only loopback addresses are ever probed, whatever the config says.
        self.siblings = {str(n): str(u) for n, u in (siblings or {}).items()
                         if local_origin(u)}
        self.timeout = timeout
        self.ttl = ttl
        self._transport = transport
        self._clock = clock
        self._lock = asyncio.Lock()
        self._at: float | None = None
        self._found: list[dict] = []

    async def found(self) -> list[dict]:
        async with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self.ttl:
                self._found = await self._probe()
                self._at = now
            return [dict(s) for s in self._found]

    async def _ask(self, client, url):
        # httpx's timeout is per phase, so a sibling that trickles its answer
        # could hold the page longer. This caps the whole exchange.
        try:
            return await asyncio.wait_for(read_health(client, url), self.timeout)
        except asyncio.TimeoutError:
            return None

    async def _probe(self) -> list[dict]:
        if not self.siblings:
            return []
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False,
                                     transport=self._transport) as client:
            names = list(self.siblings)
            answers = await asyncio.gather(
                *(self._ask(client, self.siblings[n]) for n in names))
        return [{"name": n,
                 "origin": clean_origin(data.get("browser_origin")),
                 "local": local_origin(self.siblings[n])}
                for n, data in zip(names, answers) if data is not None]
