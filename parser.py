"""
HTML parser built on stdlib html.parser.

Extracts:
  • title
  • plain text (script/style stripped, block elements normalised)
  • outbound links with their anchor text
  • <meta name="robots"> noindex / nofollow directives
  • <meta charset> and http-equiv charset for correct decoding

Returns a ParsedPage NamedTuple so callers can either destructure
(title, text, links, ...) or access fields by name.
"""
import re, logging
from html.parser import HTMLParser
from typing import List, Tuple, NamedTuple
from urllib.parse import (
    urljoin, urlparse, urlunparse, urlencode, parse_qsl
)

log = logging.getLogger(__name__)

_SKIP_TAGS = frozenset({
    "script","style","noscript","template","svg","math",
    "iframe","object","embed","canvas",
})
_BLOCK_TAGS = frozenset({
    "p","div","br","h1","h2","h3","h4","h5","h6",
    "li","td","th","tr","article","section","header",
    "footer","nav","aside","blockquote","main",
})
_HEADING_TAGS = frozenset({"h1","h2","h3"})

_SKIP_EXT = frozenset({
    ".pdf",".jpg",".jpeg",".png",".gif",".svg",".ico",".webp",
    ".mp4",".mp3",".avi",".mov",".zip",".tar",".gz",".exe",
    ".css",".js",".json",".xml",".rss",".woff",".ttf",".eot",
    ".doc",".docx",".xls",".xlsx",".ppt",".pptx",
})
_OK_SCHEMES = frozenset({"http", "https"})


# ── Return type ───────────────────────────────────────────────────────────────
class ParsedPage(NamedTuple):
    """
    Result of parse_page().  NamedTuple so it can be destructured as a
    3-tuple (title, text, links) for backward compat, while still exposing
    named fields for new code.
    """
    title:        str
    text:         str
    links:        List[str]               # deduplicated, normalised URLs only
    anchor_links: List[Tuple[str, str]]   # [(normalised_url, anchor_text), ...]
    noindex:      bool                    # <meta name="robots" content="noindex">
    nofollow:     bool                    # <meta name="robots" content="nofollow">


# ── HTML extractor ────────────────────────────────────────────────────────────
class _Extractor(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self._base        = base
        self._skip        = 0
        self._in_title    = False
        self._in_a        = False
        self._cur_href    = ""
        self._cur_anchor: List[str] = []
        self._title:      List[str] = []
        self._text:       List[str] = []
        self._raw_links:  List[Tuple[str,str]] = []  # (href, anchor_text)
        self.noindex  = False
        self.nofollow = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        d   = dict(attrs)

        if tag in _SKIP_TAGS:
            self._skip += 1
            return

        if tag == "title":
            self._in_title = True
            return

        if tag == "meta":
            name    = d.get("name", "").lower()
            content = d.get("content", "").lower()
            # charset
            if name == "robots":
                if "noindex" in content:
                    self.noindex = True
                if "nofollow" in content:
                    self.nofollow = True
            # http-equiv robots
            elif d.get("http-equiv","").lower() == "x-robots-tag":
                if "noindex" in content:
                    self.noindex = True
                if "nofollow" in content:
                    self.nofollow = True
            return

        if tag == "a":
            href = d.get("href","")
            rel  = d.get("rel","").lower()
            if href and not href.startswith(("#","javascript:","mailto:","tel:")):
                # rel="nofollow" on individual links still means we record
                # the link but the crawler may choose to skip following it.
                self._in_a     = True
                self._cur_href = href
                self._cur_anchor = []

        if tag in _BLOCK_TAGS or tag in _HEADING_TAGS:
            if not self._skip:
                self._text.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return

        if tag == "title":
            self._in_title = False
            return

        if tag == "a" and self._in_a:
            anchor = " ".join(self._cur_anchor).strip()[:200]
            if self._cur_href:
                self._raw_links.append((self._cur_href, anchor))
            self._in_a     = False
            self._cur_href = ""
            self._cur_anchor = []

        if tag in _BLOCK_TAGS or tag in _HEADING_TAGS:
            if not self._skip:
                self._text.append(" ")

    def handle_data(self, data):
        if self._skip:
            return
        s = data.strip()
        if not s:
            return
        if self._in_title:
            self._title.append(s)
        else:
            self._text.append(s)
        if self._in_a:
            self._cur_anchor.append(s)

    @property
    def title(self) -> str:
        return " ".join(self._title).strip()[:512]

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._text)).strip()

    @property
    def raw_links(self) -> List[Tuple[str,str]]:
        return self._raw_links


# ── URL helpers ───────────────────────────────────────────────────────────────
def normalize_url(url: str) -> str:
    """Normalise + validate URL.  Returns '' for unwanted URLs.

    Handles:
      • scheme and netloc lowercasing
      • default port stripping (:80, :443)
      • dot-segment resolution  (/a/../b -> /b)
      • double-slash collapsing (//path  -> /path)
      • trailing slash removal  (/about/ -> /about)
      • empty query-string drop (?       -> nothing)
      • stable query parameter order
      • fragment always stripped
    """
    try:
        p      = urlparse(url)
        scheme = p.scheme.lower()
        if scheme not in _OK_SCHEMES:
            return ""
        netloc = p.netloc.lower()
        if ":" in netloc:
            host, port = netloc.rsplit(":", 1)
            if (scheme == "http"  and port == "80") or \
               (scheme == "https" and port == "443"):
                netloc = host
        # Resolve dot segments and collapse repeated slashes
        path = _normalize_path(p.path or "/")
        # Strip trailing slash (except bare /)
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        # Drop URLs whose path has an unwanted extension
        pl = path.lower()
        if any(pl.endswith(ext) for ext in _SKIP_EXT):
            return ""
        # Stable sorted query; empty query becomes no query
        qs = urlencode(sorted(parse_qsl(p.query))) if p.query else ""
        return urlunparse((scheme, netloc, path, "", qs, ""))
    except Exception:
        return ""


def _normalize_path(path: str) -> str:
    """Resolve . and .. segments; collapse repeated leading slashes."""
    import posixpath, re
    # POSIX keeps leading // as special — collapse to single /
    path = re.sub(r"^/+", "/", path) if path else "/"
    return posixpath.normpath(path)


def same_domain(a: str, b: str) -> bool:
    try:
        ha = urlparse(a).netloc.lower().lstrip("www.")
        hb = urlparse(b).netloc.lower().lstrip("www.")
        return ha == hb
    except Exception:
        return False


# ── Main entry point ──────────────────────────────────────────────────────────
def parse_page(url: str, raw: bytes) -> ParsedPage:
    """
    Parse HTML bytes and return a ParsedPage.

    Backward-compatible: callers that destructure as
        title, text, links = parse_page(url, raw)
    still work because ParsedPage is a NamedTuple and the first three
    fields are title / text / links.
    """
    # Decode — respect meta charset
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r'charset=["\']?([a-zA-Z0-9_-]+)', text[:2_000], re.I)
    if m:
        try:
            text = raw.decode(m.group(1), errors="replace")
        except Exception:
            pass

    ex = _Extractor(url)
    try:
        ex.feed(text)
    except Exception as e:
        log.debug("parse error %s: %s", url, e)

    # Normalise & deduplicate links while preserving anchor text
    seen:         set                  = set()
    links:        List[str]            = []
    anchor_links: List[Tuple[str,str]] = []

    for href, anchor in ex.raw_links:
        n = normalize_url(urljoin(url, href))
        if n and n not in seen:
            seen.add(n)
            links.append(n)
            anchor_links.append((n, anchor))

    return ParsedPage(
        title        = ex.title,
        text         = ex.text[:200_000],
        links        = links,
        anchor_links = anchor_links,
        noindex      = ex.noindex,
        nofollow     = ex.nofollow,
    )