"""
Async HTTP fetcher — stdlib urllib inside a ThreadPoolExecutor.
No external HTTP dependencies.
"""
import asyncio, gzip, logging, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import config

log   = logging.getLogger(__name__)
_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="fetch")


class FetchResult:
    __slots__ = ("url", "final_url", "status", "content", "content_type", "error")

    def __init__(self, url: str, final_url: str, status: int,
                 content: bytes, content_type: str, error: str = ""):
        self.url          = url
        self.final_url    = final_url or url
        self.status       = status
        self.content      = content
        self.content_type = content_type
        self.error        = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.error

    @property
    def is_html(self) -> bool:
        return "text/html" in self.content_type.lower()


def _fetch_sync(url: str, timeout: int) -> FetchResult:
    """Blocking fetch — runs in thread pool."""
    req = urllib.request.Request(url, headers={
        "User-Agent":      config.USER_AGENT,
        "Accept":          "text/html,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection":      "close",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "text/html" not in ct.lower():
                return FetchResult(url, resp.url, resp.status, b"", ct)

            data, limit = b"", config.MAX_CONTENT_LENGTH
            while len(data) < limit:
                chunk = resp.read(65_536)
                if not chunk:
                    break
                data += chunk

            enc = resp.headers.get("Content-Encoding", "")
            if enc == "gzip":
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass

            return FetchResult(url, resp.url, resp.status, data, ct)

    except urllib.error.HTTPError as e:
        return FetchResult(url, url, e.code, b"", "", str(e))
    except urllib.error.URLError as e:
        return FetchResult(url, url, 0, b"", "", str(e.reason))
    except Exception as e:
        return FetchResult(url, url, 0, b"", "", str(e))


async def fetch(url: str, retries: int = config.MAX_RETRIES) -> FetchResult:
    loop = asyncio.get_event_loop()
    last: Optional[FetchResult] = None
    for attempt in range(retries + 1):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        try:
            result = await loop.run_in_executor(
                _POOL, _fetch_sync, url, config.FETCH_TIMEOUT_SECONDS
            )
            # terminal statuses — don't retry
            if result.ok or result.status in (401, 403, 404, 410):
                return result
            last = result
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last = FetchResult(url, url, 0, b"", "", str(e))
            log.debug("attempt %d failed for %s: %s", attempt + 1, url, e)
    return last or FetchResult(url, url, 0, b"", "", "max retries exceeded")
