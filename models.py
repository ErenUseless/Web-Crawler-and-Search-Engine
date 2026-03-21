from dataclasses import dataclass, field
from typing import Optional
import time


@dataclass
class Page:
    url:          str
    depth:        int
    id:           Optional[int] = None
    url_hash:     str = ""
    content_hash: str = ""
    title:        str = ""
    text_content: str = ""
    status:       str = "pending"   # pending|fetching|done|error|skipped
    error_msg:    str = ""
    fetched_at:   float = 0.0
    indexed:      bool  = False
    word_count:   int   = 0
    http_status:  int   = 0


@dataclass
class QueueItem:
    url:      str
    depth:    int
    id:       Optional[int] = None
    added_at: float = field(default_factory=time.time)


@dataclass
class SearchResult:
    url:     str
    title:   str
    snippet: str
    score:   float
    depth:   int = 0
    page_id: int = 0


@dataclass
class CrawlStatus:
    is_running:    bool  = False
    origin_url:    str   = ""
    max_depth:     int   = 0
    pages_fetched: int   = 0
    pages_indexed: int   = 0
    pages_queued:  int   = 0
    pages_failed:  int   = 0
    pages_skipped: int   = 0
    start_time:    float = 0.0
    end_time:      float = 0.0   # set when crawl finishes; 0 means still running

    @property
    def elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0.0
        stop = self.end_time if self.end_time else time.time()
        return stop - self.start_time

    @property
    def fetch_rate(self) -> float:
        e = self.elapsed_seconds
        return self.pages_fetched / e if e > 0 else 0.0