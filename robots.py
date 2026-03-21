"""
robots.txt compliance layer.

Parses and caches robots.txt per domain.  Respects:
  • User-agent: * and User-agent: <our-agent>
  • Disallow / Allow directives (Allow takes precedence when more specific)
  • Crawl-delay directive (stored and surfaced via .crawl_delay())
  • Cache TTL so we don't re-fetch on every URL

Intentionally uses only stdlib — no robotparser workarounds needed here.
"""
import asyncio, logging, time, urllib.parse, urllib.request, urllib.error
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import config

log = logging.getLogger(__name__)

# How long to cache a domain's robots.txt (seconds)
ROBOTS_CACHE_TTL = 3_600   # 1 hour
# Timeout for fetching robots.txt
ROBOTS_TIMEOUT   = 8


@dataclass
class _RobotsData:
    """Compiled rules for a single domain."""
    disallowed:   List[str]        # prefix paths that are forbidden
    allowed:      List[str]        # explicit Allow overrides
    crawl_delay:  float            # 0 means "use global config"
    fetched_at:   float = field(default_factory=time.time)
    fetch_failed: bool  = False    # True if robots.txt could not be retrieved

    def is_allowed(self, path: str) -> bool:
        """True if this path is allowed to be crawled."""
        if self.fetch_failed:
            # RFC: if we can't reach robots.txt assume allowed
            return True

        # Find the longest matching Allow rule
        best_allow_len    = 0
        best_disallow_len = 0

        for rule in self.allowed:
            if path.startswith(rule) and len(rule) > best_allow_len:
                best_allow_len = len(rule)

        for rule in self.disallowed:
            if path.startswith(rule) and len(rule) > best_disallow_len:
                best_disallow_len = len(rule)

        if best_allow_len == 0 and best_disallow_len == 0:
            return True   # no matching rule → allowed

        # Longer (more specific) rule wins; Allow wins ties
        return best_allow_len >= best_disallow_len


# ── Parser ────────────────────────────────────────────────────────────────────
def _parse_robots(body: str, our_agent: str) -> _RobotsData:
    """
    Parse robots.txt text.  Returns rules relevant to our agent.
    our_agent should be the token portion of config.USER_AGENT (lower-case).

    Handles both blank-line separated groups AND non-blank-line separated groups
    (many real sites omit blank lines between agent groups).
    """
    our_agent = our_agent.lower()
    lines     = body.splitlines()

    # Collect rule groups: { agent_name: (disallowed, allowed, crawl_delay) }
    groups: Dict[str, Tuple[List[str], List[str], float]] = {}
    current_agents: List[str] = []
    in_rule_section = False   # True once we've seen the first rule for this group

    for raw in lines:
        line = raw.split("#", 1)[0].strip()

        if not line:
            # Blank line always terminates the current group
            current_agents = []
            in_rule_section = False
            continue

        if ":" not in line:
            continue

        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()

        if key == "user-agent":
            agent = val.lower()
            # If we were already in a rule section, a new User-agent line
            # starts a new group — reset the agent list
            if in_rule_section:
                current_agents = []
                in_rule_section = False
            current_agents.append(agent)
            if agent not in groups:
                groups[agent] = ([], [], 0.0)

        elif key == "disallow":
            in_rule_section = True
            for agent in current_agents:
                if agent not in groups:
                    groups[agent] = ([], [], 0.0)
                if val:   # empty Disallow means "allow everything"
                    groups[agent][0].append(val)

        elif key == "allow":
            in_rule_section = True
            for agent in current_agents:
                if agent not in groups:
                    groups[agent] = ([], [], 0.0)
                if val:
                    groups[agent][1].append(val)

        elif key == "crawl-delay":
            in_rule_section = True
            try:
                delay = float(val)
                for agent in current_agents:
                    d, a, _ = groups.get(agent, ([], [], 0.0))
                    groups[agent] = (d, a, delay)
            except ValueError:
                pass

    # Merge: specific agent rules override wildcard
    wildcard = groups.get("*",       ([], [], 0.0))
    specific = groups.get(our_agent, None)

    if specific:
        dis, alw, delay = specific
    else:
        dis, alw, delay = wildcard

    return _RobotsData(disallowed=dis, allowed=alw, crawl_delay=delay)


# ── Checker ───────────────────────────────────────────────────────────────────
class RobotsChecker:
    """
    Async robots.txt checker.  One instance is shared by the whole crawler.
    Thread-safe via asyncio.Lock per domain.
    """

    def __init__(self):
        self._cache:  Dict[str, _RobotsData] = {}
        self._locks:  Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        # Extract agent token from config (e.g. "SingleNodeCrawler" → "singlenode")
        token = config.USER_AGENT.split("/")[0].lower().replace(" ", "")
        self._agent = token

    # ── Public ────────────────────────────────────────────────────────────────
    async def allowed(self, url: str) -> bool:
        """Return True if the URL may be crawled per robots.txt."""
        try:
            p     = urllib.parse.urlparse(url)
            root  = f"{p.scheme}://{p.netloc}"
            path  = p.path or "/"
            data  = await self._get(root)
            return data.is_allowed(path)
        except Exception:
            return True   # fail open

    async def crawl_delay(self, url: str) -> float:
        """Return the crawl-delay for this URL's domain (0 if not specified)."""
        try:
            p    = urllib.parse.urlparse(url)
            root = f"{p.scheme}://{p.netloc}"
            data = await self._get(root)
            return data.crawl_delay
        except Exception:
            return 0.0

    async def prefetch(self, url: str) -> None:
        """Eagerly fetch robots.txt for a domain (fire-and-forget)."""
        asyncio.create_task(self.allowed(url))

    # ── Internal ──────────────────────────────────────────────────────────────
    async def _get_lock(self, domain: str) -> asyncio.Lock:
        async with self._global_lock:
            if domain not in self._locks:
                self._locks[domain] = asyncio.Lock()
            return self._locks[domain]

    async def _get(self, root: str) -> _RobotsData:
        """Return cached (or freshly fetched) robots data for root."""
        cached = self._cache.get(root)
        if cached and (time.time() - cached.fetched_at) < ROBOTS_CACHE_TTL:
            return cached

        lock = await self._get_lock(root)
        async with lock:
            # Double-check after acquiring lock
            cached = self._cache.get(root)
            if cached and (time.time() - cached.fetched_at) < ROBOTS_CACHE_TTL:
                return cached
            data = await self._fetch(root)
            self._cache[root] = data
            return data

    async def _fetch(self, root: str) -> _RobotsData:
        """Download and parse robots.txt for root.  Never raises."""
        robots_url = f"{root}/robots.txt"
        log.debug("Fetching robots.txt  %s", robots_url)
        loop = asyncio.get_event_loop()
        try:
            body = await asyncio.wait_for(
                loop.run_in_executor(None, self._fetch_sync, robots_url),
                timeout=ROBOTS_TIMEOUT,
            )
            if body is None:
                log.debug("robots.txt not found at %s — assuming allow-all", root)
                return _RobotsData(disallowed=[], allowed=[], crawl_delay=0.0,
                                   fetch_failed=True)
            data = _parse_robots(body, self._agent)
            log.debug("robots.txt for %s — %d disallow, %d allow, delay=%.1f",
                      root, len(data.disallowed), len(data.allowed),
                      data.crawl_delay)
            return data
        except Exception as e:
            log.debug("robots.txt fetch error %s: %s", root, e)
            return _RobotsData(disallowed=[], allowed=[], crawl_delay=0.0,
                               fetch_failed=True)

    @staticmethod
    def _fetch_sync(url: str) -> Optional[str]:
        """Blocking fetch of robots.txt.  Returns None on 4xx/error."""
        req = urllib.request.Request(url, headers={
            "User-Agent": config.USER_AGENT,
            "Connection": "close",
        })
        try:
            with urllib.request.urlopen(req, timeout=ROBOTS_TIMEOUT) as resp:
                if resp.status == 200:
                    raw = resp.read(256_000)
                    return raw.decode("utf-8", errors="replace")
                return None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Treat auth errors as "disallow all" — conservative
                return "User-agent: *\nDisallow: /\n"
            return None    # 404, 410, etc. → allow all
        except Exception:
            return None
