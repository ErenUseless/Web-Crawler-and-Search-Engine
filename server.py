"""
Minimal asyncio HTTP/1.1 server — zero web-framework dependencies.

Endpoints
─────────
GET  /            HTML UI (dark-themed, single page)
GET  /status      JSON crawl stats
POST /crawl       Start a new crawl  body: {url, depth, same_domain}
POST /stop        Abort running crawl
GET  /search?q=   TF-IDF search      params: q, limit, domain
"""
import asyncio, json, logging, math, time, urllib.parse
from typing import Optional

import config
from storage import Database
from crawler import Crawler
from indexer import Indexer
from search_engine import SearchEngine
from queue_manager import QueueManager

log = logging.getLogger(__name__)

# ── Dark HTML UI ──────────────────────────────────────────────────────────────
_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>🕷 Web Crawler</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0f;--card:#141417;--border:#222228;
  --green:#00e896;--blue:#5b9cf6;--red:#e85c5c;
  --text:#d0d0d8;--muted:#606070;--yellow:#f5c842;
}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,monospace;padding:28px 32px;max-width:1100px;margin:auto}
h1{font-size:1.55rem;font-weight:700;color:var(--green);margin-bottom:22px;letter-spacing:-.5px}
h2{font-size:.88rem;font-weight:600;color:var(--blue);text-transform:uppercase;letter-spacing:.08em;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px 22px;margin-bottom:18px}
label{font-size:.83rem;color:var(--muted);margin-right:6px}
input[type=url],input[type=text],input[type=number]{
  background:#0d0d0f;color:var(--text);border:1px solid var(--border);
  padding:8px 11px;border-radius:6px;outline:none;font-size:.88rem;
  transition:border .15s}
input:focus{border-color:var(--green)}
input[type=checkbox]{accent-color:var(--green);cursor:pointer}
button{padding:8px 20px;border:none;border-radius:6px;font-size:.88rem;font-weight:700;cursor:pointer;transition:opacity .15s}
button:hover{opacity:.85}
.btn-go{background:var(--green);color:#000}
.btn-stop{background:var(--red);color:#fff}
.btn-search{background:var(--blue);color:#fff}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
/* stats grid */
.stats{display:flex;flex-wrap:wrap;gap:6px 20px;margin-top:10px}
.stat{font-size:.85rem;color:var(--muted)}
.stat b{color:var(--green);font-size:1rem}
/* pills */
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.72rem;font-weight:700}
.pill-on {background:#00e89622;color:var(--green);border:1px solid var(--green)}
.pill-off{background:#33333340;color:var(--muted);border:1px solid var(--border)}
.pill-bp {background:#f5c84222;color:var(--yellow);border:1px solid var(--yellow)}
/* origin bar */
#origin-bar{font-size:.78rem;color:var(--muted);margin-bottom:6px;word-break:break-all}
/* search results */
.res{border-left:3px solid var(--green);padding:10px 14px;margin:8px 0;background:#0f110f;border-radius:0 6px 6px 0}
.res a{color:var(--blue);text-decoration:none;font-weight:600}
.res a:hover{text-decoration:underline}
.res-meta{color:var(--muted);font-size:.75rem;margin-top:3px}
.res-snippet{color:#b0b0c0;font-size:.85rem;margin-top:5px;line-height:1.5}
.res-url{color:#3a3a50;font-size:.75rem;margin-top:3px;word-break:break-all}
#search-summary{color:var(--muted);font-size:.82rem;margin-bottom:8px}
/* progress bar */
.pbar-wrap{height:4px;background:#1a1a22;border-radius:2px;margin-top:10px;overflow:hidden}
.pbar{height:100%;background:var(--green);border-radius:2px;transition:width .6s ease;width:0}
#url-input{min-width:300px}
#q{min-width:280px}
</style>
</head>
<body>

<h1>🕷 Single-Node Web Crawler</h1>

<!-- Crawl control ─────────────────────────────────────────────────────────-->
<div class="card">
  <h2>Crawl</h2>
  <div class="row">
    <label>URL</label>
    <input id="url-input" type="url" placeholder="https://example.com" value="https://example.com"/>
    <label>Depth</label>
    <input id="depth" type="number" value="2" min="0" max="8" style="width:60px"/>
    <label><input type="checkbox" id="sd" checked> Same domain</label>
    <label><input type="checkbox" id="resume" onchange="onResumeToggle()"> Resume</label>
    <span id="resume-note" style="font-size:.78rem;color:var(--muted);margin-left:4px"></span>
    <button class="btn-go"  onclick="startCrawl()">▶ Start</button>
    <button class="btn-stop" onclick="stopCrawl()">■ Stop</button>
  </div>
</div>

<!-- Status ─────────────────────────────────────────────────────────────────-->
<div class="card">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">
    <h2 style="margin:0">Status</h2>
    <span id="state-pill" class="pill pill-off">IDLE</span>
    <span id="bp-pill"    class="pill pill-bp"  style="display:none">BACKPRESSURE</span>
  </div>
  <div id="origin-bar"></div>
  <div class="stats" id="stats-grid">—</div>
  <div class="pbar-wrap"><div class="pbar" id="pbar"></div></div>
</div>

<!-- Search ──────────────────────────────────────────────────────────────────-->
<div class="card">
  <h2>Search</h2>
  <div class="row">
    <input id="q" type="text" placeholder="Search indexed pages…"
           onkeydown="if(event.key==='Enter')doSearch()"/>
    <button class="btn-search" onclick="doSearch()">🔍 Search</button>
  </div>
  <div id="search-summary" style="margin-top:10px"></div>
  <div id="results"></div>
</div>

<script>
/* ── helpers ─────────────────────────────────────────────────────────────── */
const $=id=>document.getElementById(id);

/* ── crawl control ───────────────────────────────────────────────────────── */
async function startCrawl(){
  const isResume=$('resume').checked;
  const url=$('url-input').value.trim();
  if(!isResume&&!url){alert('Enter a URL');return;}
  const r=await fetch('/crawl',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      url:   isResume?'':url,   // server ignores url on resume; send empty
      depth: +$('depth').value,
      same_domain: $('sd').checked,
      resume: isResume
    })});
  const d=await r.json();
  showToast(d.message||d.error);
  tick();   // immediate update; tick() will adjust poll rate automatically
}

// When resume is toggled, lock/unlock the URL and settings fields
// and show what origin will be used
function onResumeToggle(){
  const checked=$('resume').checked;
  $('url-input').disabled=checked;
  $('depth').disabled=checked;
  $('sd').disabled=checked;
  if(checked){
    // Fetch the saved origin from the last crawl to show the user
    fetch('/status').then(r=>r.json()).then(d=>{
      if(d.origin_url){
        $('url-input').value=d.origin_url;
        $('resume-note').textContent='Will continue crawl of: '+d.origin_url;
      } else {
        $('resume-note').textContent='No previous crawl found — will start fresh';
      }
    }).catch(()=>{});
  } else {
    $('resume-note').textContent='';
    $('url-input').disabled=false;
  }
}
async function stopCrawl(){
  const d=await (await fetch('/stop',{method:'POST'})).json();
  showToast(d.message);
}

/* ── search ──────────────────────────────────────────────────────────────── */
async function doSearch(){
  const q=$('q').value.trim();
  if(!q)return;
  $('search-summary').textContent='Searching…';
  $('results').innerHTML='';
  const d=await (await fetch('/search?q='+encodeURIComponent(q)+'&limit=20')).json();
  if(!d.results||!d.results.length){
    $('search-summary').textContent='No results for "'+q+'".';return;}
  $('search-summary').textContent=d.count+' result(s) for "'+q+'" — '+d.elapsed_ms+'ms';
  $('results').innerHTML=d.results.map(r=>`
    <div class="res">
      <div><a href="${r.url}" target="_blank" rel="noopener">${esc(r.title||r.url)}</a></div>
      <div class="res-meta">Score: ${r.score.toFixed(5)} &nbsp;·&nbsp; Depth: ${r.depth}</div>
      <div class="res-snippet">${esc(r.snippet)}</div>
      <div class="res-url">${esc(r.url)}</div>
    </div>`).join('');
}

/* ── status ticker ───────────────────────────────────────────────────────── */
let _wasRunning = false;   // tracks previous running state to detect transitions

async function tick(){
  try{
    const d=await (await fetch('/status')).json();
    const running=d.is_running;

    /* pill */
    $('state-pill').textContent=running?'RUNNING':'IDLE';
    $('state-pill').className='pill '+(running?'pill-on':'pill-off');
    $('bp-pill').style.display=d.backpressure?'inline-block':'none';

    /* origin */
    $('origin-bar').textContent=d.origin_url||'';

    /* stats — always update so the final numbers are visible after completion */
    const elapsed=d.elapsed_seconds||0;
    const rate=elapsed>0?(d.pages_fetched/elapsed).toFixed(2):'0.00';
    $('stats-grid').innerHTML=
      stat('Fetched',  d.pages_fetched)+
      stat('Indexed',  d.pages_indexed)+
      stat('Queued',   d.pages_queued)+
      stat('Failed',   d.pages_failed)+
      stat('Skipped',  d.pages_skipped)+
      stat('Terms',    d.index_terms||0)+
      stat('Elapsed',  elapsed.toFixed(0)+'s')+
      stat('Rate',     rate+' p/s');

    /* progress bar */
    const total=d.pages_fetched+d.pages_queued||1;
    $('pbar').style.width=Math.min(100,(d.pages_fetched/total*100)).toFixed(1)+'%';

    /* Adaptive polling:
       - While running: poll every 1.8 s (fast updates)
       - After a running→idle transition: do ONE final tick to freeze the stats,
         then switch to slow 10 s polling (keep page fresh without hammering server)
       - Always idle (page load with no crawl): just stay on slow polling */
    if(_wasRunning && !running){
      // Crawl just finished — slow down
      clearInterval(_pollTimer);
      _pollTimer = setInterval(tick, 10000);
    } else if(!_wasRunning && running){
      // Crawl just started — speed up
      clearInterval(_pollTimer);
      _pollTimer = setInterval(tick, 1800);
    }
    _wasRunning = running;
  }catch(e){}
}
const stat=(lbl,val)=>`<div class="stat">${lbl} <b>${val}</b></div>`;

/* ── toast ────────────────────────────────────────────────────────────────── */
function showToast(msg){
  const t=document.createElement('div');
  Object.assign(t.style,{
    position:'fixed',bottom:'24px',right:'24px',background:'#1a1a2a',
    color:'#d0d0e0',padding:'12px 20px',borderRadius:'8px',
    border:'1px solid #333',fontSize:'.88rem',zIndex:9999,
    boxShadow:'0 4px 16px #0008'});
  t.textContent=msg;document.body.appendChild(t);
  setTimeout(()=>t.remove(),4000);
}

function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Start with slow polling; speeds up automatically once a crawl begins
let _pollTimer = setInterval(tick, 10000);
tick();   // immediate first update
</script>
</body>
</html>"""


class WebServer:
    def __init__(self, db: Database, crawler: Crawler,
                 search: SearchEngine, indexer: Indexer):
        self.db      = db
        self.crawler = crawler
        self.search  = search
        self.indexer = indexer
        self._crawl_task: Optional[asyncio.Task] = None
        self._index_task: Optional[asyncio.Task] = None

    async def start(self):
        self._index_task = asyncio.create_task(
            self.indexer.run_continuous(), name="indexer"
        )
        server = await asyncio.start_server(
            self._handle, config.SERVER_HOST, config.SERVER_PORT
        )
        log.info("HTTP server  →  http://localhost:%d", config.SERVER_PORT)
        async with server:
            await server.serve_forever()

    # ── Raw HTTP plumbing ─────────────────────────────────────────────────────
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter):
        try:
            raw_line = await asyncio.wait_for(reader.readline(), 8)
            if not raw_line:
                return
            parts = raw_line.decode(errors="replace").split()
            if len(parts) < 2:
                return
            method, full_path = parts[0].upper(), parts[1]

            # consume headers
            headers: dict = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), 5)
                if line in (b"\r\n", b"\n", b""):
                    break
                if b":" in line:
                    k, _, v = line.decode(errors="replace").partition(":")
                    headers[k.strip().lower()] = v.strip()

            # read body
            body = b""
            cl   = int(headers.get("content-length", 0))
            if cl > 0:
                body = await asyncio.wait_for(
                    reader.readexactly(min(cl, 65_536)), 5
                )

            path, _, qs = full_path.partition("?")
            params      = dict(urllib.parse.parse_qsl(qs))

            result = await self._route(method, path, params, body)
            data, status, ct = result[0], result[1], result[2]

            extra = result[3] if len(result) > 3 else {}
            extra_lines = "".join(f"{k}: {v}\r\n" for k, v in extra.items())
            response = (
                f"HTTP/1.1 {status}\r\n"
                f"Content-Type: {ct}\r\n"
                f"Content-Length: {len(data)}\r\n"
                f"Connection: close\r\n"
                f"Access-Control-Allow-Origin: *\r\n"
                f"{extra_lines}\r\n"
            ).encode() + data
            writer.write(response)
            await writer.drain()

        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.debug("handler error: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _route(self, method: str, path: str,
                     params: dict, body: bytes):
        """Dispatch to the correct handler. Returns (bytes, status_str, ct)."""
        try:
            if   path == "/"        and method == "GET":  return await self._ui()
            elif path == "/status"  and method == "GET":  return await self._status()
            elif path == "/crawl"   and method == "POST": return await self._crawl(body)
            elif path == "/stop"    and method == "POST": return await self._stop()
            elif path == "/search"  and method == "GET":  return await self._search(params)
            elif path == "/pages"   and method == "GET":  return await self._pages(params)
            elif path == "/export"  and method == "GET":  return await self._export(params)
            return _json({"error": "not found"}), "404 Not Found", "application/json"
        except Exception as e:
            log.exception("route error")
            return (_json({"error": str(e)}),
                    "500 Internal Server Error", "application/json")

    # ── Handlers ──────────────────────────────────────────────────────────────
    async def _ui(self):
        return _UI.encode(), "200 OK", "text/html; charset=utf-8"

    async def _status(self):
        s     = await self.crawler.get_status()
        stats = await self.db.get_stats()
        # Show saved origin even when idle so the UI can display it
        # in the Resume note before a crawl starts
        origin = s.origin_url or (await self.db.get_meta("origin_url") or "")
        return _json({
            "is_running":     s.is_running,
            "origin_url":     origin,
            "max_depth":      s.max_depth,
            "pages_fetched":  s.pages_fetched,
            "pages_indexed":  s.pages_indexed,
            "pages_queued":   s.pages_queued,
            "pages_failed":   s.pages_failed,
            "pages_skipped":  s.pages_skipped,
            "elapsed_seconds": round(s.elapsed_seconds, 1),
            "end_time":       s.end_time,
            "fetch_rate":     round(s.fetch_rate, 3),
            "index_terms":    stats.get("index_terms", 0),
            "backpressure":   self.crawler.queue.backpressure_active,
        }), "200 OK", "application/json"

    async def _crawl(self, body: bytes):
        try:
            data = json.loads(body)
        except Exception:
            return (_json({"error": "invalid JSON"}),
                    "400 Bad Request", "application/json")

        resume = bool(data.get("resume", False))
        url    = str(data.get("url", "")).strip()
        depth  = max(0, min(int(data.get("depth", 2)), 10))
        sd     = bool(data.get("same_domain", True))

        if not resume and not url:
            return (_json({"error": "url is required"}),
                    "400 Bad Request", "application/json")

        if resume:
            # On resume, retrieve the saved origin from DB so we can echo it
            # back in the response message. The crawler itself also reads it
            # from DB — the url argument is intentionally ignored.
            saved = await self.db.get_meta("origin_url")
            if not saved:
                # No previous crawl — treat as fresh start with the given URL
                resume = False
                if not url:
                    return (_json({"error": "no previous crawl found and no url given"}),
                            "400 Bad Request", "application/json")
            else:
                url = saved   # for the response message only

        if self.crawler.is_running:
            return (_json({"error": "a crawl is already running"}),
                    "409 Conflict", "application/json")

        self._crawl_task = asyncio.create_task(
            self.crawler.start(url, depth, sd, resume=resume), name="crawl"
        )
        mode = "resumed" if resume else "started"
        return (_json({"message": f"Crawl {mode} \u2192 {url}  depth={depth}"}),
                "202 Accepted", "application/json")

    async def _stop(self):
        if not self.crawler.is_running:
            return (_json({"message": "no crawl is running"}),
                    "200 OK", "application/json")
        self.crawler.stop()
        if self._crawl_task:
            self._crawl_task.cancel()
        return _json({"message": "Crawl stopped"}), "200 OK", "application/json"

    async def _pages(self, params: dict):
        """Paginated browse of all crawled pages."""
        try:
            page_num = max(1, int(params.get("page", 1)))
            per_page = min(50, max(1, int(params.get("per_page", 20))))
            status   = params.get("status", "done")
        except (ValueError, TypeError):
            return (_json({"error": "invalid params"}),
                    "400 Bad Request", "application/json")

        rows, total = await self.db.list_pages(
            status=status, page=page_num, per_page=per_page
        )
        total_pages = math.ceil(total / per_page) if total else 0
        return _json({
            "page":          page_num,
            "per_page":      per_page,
            "total":         total,
            "total_pages":   total_pages,
            "status_filter": status,
            "items": [{
                "id":          r["id"],
                "url":         r["url"],
                "title":       r["title"],
                "depth":       r["depth"],
                "word_count":  r["word_count"],
                "indexed":     bool(r["indexed"]),
                "http_status": r["http_status"],
                "fetched_at":  r["fetched_at"],
            } for r in rows],
        }), "200 OK", "application/json"

    async def _export(self, params: dict):
        """Export all indexed pages as JSON Lines or CSV."""
        import csv, io
        fmt = params.get("format", "jsonl").lower()
        if fmt not in ("jsonl", "csv"):
            return (_json({"error": "format must be jsonl or csv"}),
                    "400 Bad Request", "application/json")

        rows = await self.db.export_pages()

        if not rows:
            return (b"", "204 No Content", "application/json", {})

        if fmt == "jsonl":
            lines = [
                json.dumps({
                    "url":        r["url"],
                    "title":      r["title"],
                    "depth":      r["depth"],
                    "word_count": r["word_count"],
                    "http_status": r["http_status"],
                    "fetched_at": r["fetched_at"],
                }, ensure_ascii=False)
                for r in rows
            ]
            body = ("\n".join(lines) + "\n").encode()
            headers = {"Content-Disposition": "attachment; filename=\"pages.jsonl\""}
            return body, "200 OK", "application/x-ndjson", headers

        else:  # csv
            buf = io.StringIO()
            w   = csv.writer(buf)
            w.writerow(["url", "title", "depth", "word_count",
                        "http_status", "fetched_at"])
            for r in rows:
                w.writerow([r["url"], r["title"], r["depth"],
                            r["word_count"], r["http_status"], r["fetched_at"]])
            body    = buf.getvalue().encode()
            headers = {"Content-Disposition": "attachment; filename=\"pages.csv\""}
            return body, "200 OK", "text/csv; charset=utf-8", headers

    async def _search(self, params: dict):
        q     = params.get("q", "").strip()
        limit = min(int(params.get("limit", 20)), 50)
        domain = params.get("domain", "").strip() or None

        if not q:
            return (_json({"results": [], "query": ""}),
                    "200 OK", "application/json")

        t0      = time.monotonic()
        results = await self.search.search(q, limit=limit, domain_filter=domain)
        elapsed = round((time.monotonic() - t0) * 1000, 2)

        return _json({
            "query":      q,
            "elapsed_ms": elapsed,
            "count":      len(results),
            "results": [{
                "url":     r.url,
                "title":   r.title,
                "snippet": r.snippet,
                "score":   round(r.score, 6),
                "depth":   r.depth,
            } for r in results],
        }), "200 OK", "application/json"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _json(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode()