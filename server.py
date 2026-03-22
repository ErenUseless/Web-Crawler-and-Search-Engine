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
<html class="dark" lang="en">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>Single-Node Web Crawler</title>
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet"/>
<script id="tailwind-config">
tailwind.config = {
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        "surface-tint":"#6dfe9c","inverse-primary":"#006e36","surface-variant":"#262528",
        "secondary-fixed-dim":"#64ecaf","on-error":"#490006","primary-fixed":"#6dfe9c",
        "surface-container-low":"#131315","on-tertiary-container":"#004956","error-dim":"#d7383b",
        "secondary-container":"#006c48","on-secondary-fixed-variant":"#006946",
        "tertiary-fixed-dim":"#00cded","on-secondary-container":"#e1ffeb",
        "surface-container-high":"#1f1f22","surface-dim":"#0e0e10",
        "on-primary-fixed-variant":"#006a34","on-primary-fixed":"#004a22",
        "on-tertiary-fixed":"#00333d","surface-container-lowest":"#000000",
        "on-primary":"#005f2e","tertiary-fixed":"#00dcfe","primary-fixed-dim":"#5def8f",
        "tertiary-container":"#00dcfe","error-container":"#9f0519","secondary-fixed":"#73fbbc",
        "primary-dim":"#5def8f","surface-bright":"#2c2c2f","on-secondary-fixed":"#00492f",
        "outline-variant":"#48474a","primary":"#6dfe9c","background":"#0e0e10",
        "on-tertiary-fixed-variant":"#005361","secondary":"#73fbbc","outline":"#767577",
        "surface-container-highest":"#262528","inverse-on-surface":"#565457",
        "on-primary-container":"#002f13","on-surface":"#f9f5f8","inverse-surface":"#fcf8fb",
        "error":"#ff716c","tertiary-dim":"#00cded","surface":"#0e0e10",
        "on-background":"#f9f5f8","on-secondary":"#005e3e","tertiary":"#7ce6ff",
        "primary-container":"#19be64","surface-container":"#19191c","secondary-dim":"#64ecaf",
        "on-tertiary":"#005361","on-surface-variant":"#adaaad","on-error-container":"#ffa8a3"
      },
      fontFamily: {
        "headline":["Space Grotesk"],"body":["Inter"],"label":["Inter"]
      },
      borderRadius: {"DEFAULT":"0.125rem","lg":"0.25rem","xl":"0.5rem","full":"0.75rem"},
    },
  },
}
</script>
<style>
.material-symbols-outlined { font-variation-settings:'FILL' 0,'wght' 400,'GRAD' 0,'opsz' 24; }
.glow-primary { box-shadow: 0 0 20px rgba(109,254,156,0.15); }
</style>
</head>
<body class="bg-background text-on-surface font-body selection:bg-primary/30 min-h-screen">

<main class="w-full py-12 px-8 min-h-screen">
<div class="max-w-7xl mx-auto space-y-12">

<!-- Header -->
<div class="flex items-center gap-4 border-b border-outline-variant/20 pb-8">
  <span class="text-2xl font-bold text-[#f9f5f8] tracking-tighter font-headline">Single-Node Web Crawler</span>
  <div class="h-5 w-[1px] bg-outline-variant/30"></div>
  <span id="origin-bar" class="text-[12px] font-label uppercase tracking-[0.2em] text-on-surface-variant"></span>
</div>

<!-- Row 1: CRAWL ENGINE + STATUS -->
<div class="grid grid-cols-1 lg:grid-cols-12 gap-8">

  <!-- CRAWL ENGINE -->
  <section class="lg:col-span-5 bg-surface-container-low p-8 rounded-xl space-y-8 border border-outline-variant/10">
    <div class="flex items-center justify-between">
      <h3 class="font-headline font-bold text-xl text-on-surface">CRAWL ENGINE</h3>
      <span id="engine-badge" class="text-[10px] font-label bg-primary/10 text-primary px-2 py-1 rounded">READY</span>
    </div>
    <div class="space-y-6">
      <div class="space-y-2">
        <label class="text-[10px] font-label text-on-surface-variant uppercase tracking-widest">Target Entry Point</label>
        <input id="url-input" class="w-full bg-surface-container-highest border-none rounded-sm text-sm p-3 focus:ring-1 focus:ring-primary/60 placeholder:text-on-surface-variant/30"
               placeholder="https://example.com" type="url"/>
        <p id="resume-note" class="text-[10px] text-primary font-label mt-1 hidden"></p>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <div class="space-y-2">
          <label class="text-[10px] font-label text-on-surface-variant uppercase tracking-widest">Max Depth</label>
          <select id="depth" class="w-full bg-surface-container-highest border-none rounded-sm text-sm p-3 focus:ring-1 focus:ring-primary/60">
            <option value="1">Depth 1</option>
            <option value="2" selected>Depth 2</option>
            <option value="3">Depth 3</option>
            <option value="5">Depth 5</option>
          </select>
        </div>
        <div class="flex flex-col justify-end gap-3 pb-1">
          <label class="flex items-center gap-2 cursor-pointer group">
            <input id="sd" checked type="checkbox"
                   class="rounded-sm bg-surface-container-highest border-none text-primary-container focus:ring-offset-background focus:ring-primary"/>
            <span class="text-[11px] font-label text-on-surface-variant group-hover:text-on-surface transition-colors">Same domain</span>
          </label>
          <label class="flex items-center gap-2 cursor-pointer group">
            <input id="resume" type="checkbox" onchange="onResumeToggle()"
                   class="rounded-sm bg-surface-container-highest border-none text-primary-container focus:ring-offset-background focus:ring-primary"/>
            <span class="text-[11px] font-label text-on-surface-variant group-hover:text-on-surface transition-colors">Resume session</span>
          </label>
        </div>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-4 pt-4">
      <button onclick="startCrawl()"
              class="bg-primary-container text-on-primary-container py-4 rounded-md font-headline font-bold tracking-tight hover:brightness-110 transition-all flex items-center justify-center gap-2">
        <span class="material-symbols-outlined" style="font-variation-settings:'FILL' 1;">play_arrow</span>
        START
      </button>
      <button onclick="stopCrawl()"
              class="bg-surface-container-highest text-on-surface py-4 rounded-md font-headline font-bold tracking-tight border border-outline-variant/10 hover:bg-error/10 hover:text-error transition-all flex items-center justify-center gap-2">
        <span class="material-symbols-outlined" style="font-variation-settings:'FILL' 1;">stop</span>
        STOP
      </button>
    </div>
  </section>

  <!-- STATUS / TELEMETRY -->
  <section class="lg:col-span-7 bg-surface-container-low p-8 rounded-xl flex flex-col border border-outline-variant/10">
    <div class="flex items-center justify-between mb-8">
      <div class="flex items-center gap-3">
        <div id="state-dot" class="w-2 h-2 rounded-full bg-outline-variant"></div>
        <h3 class="font-headline font-bold text-xl text-on-surface uppercase tracking-tight">Active Telemetry</h3>
      </div>
      <div class="text-right">
        <p class="text-[10px] font-label text-on-surface-variant uppercase tracking-widest">STATUS</p>
        <p id="state-text" class="text-on-surface-variant font-headline font-bold">IDLE</p>
      </div>
    </div>
    <!-- Progress Bar -->
    <div class="mb-10">
      <div class="flex justify-between items-end mb-3">
        <span class="text-[10px] font-label text-on-surface-variant uppercase tracking-widest">Processing Node Index</span>
        <span id="pbar-pct" class="text-xs font-headline font-bold text-primary">0%</span>
      </div>
      <div class="w-full h-1.5 bg-surface-container-highest rounded-full overflow-hidden">
        <div id="pbar" class="h-full bg-primary glow-primary transition-all duration-700" style="width:0%"></div>
      </div>
    </div>
    <!-- Stats Grid -->
    <div class="grid grid-cols-4 gap-y-10 gap-x-4">
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Fetched</p>
        <p id="stat-fetched" class="text-2xl font-headline font-bold text-on-surface tracking-tighter">—</p>
      </div>
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Indexed</p>
        <p id="stat-indexed" class="text-2xl font-headline font-bold text-on-surface tracking-tighter">—</p>
      </div>
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Queued</p>
        <p id="stat-queued" class="text-2xl font-headline font-bold text-on-surface tracking-tighter">—</p>
      </div>
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Failed</p>
        <p id="stat-failed" class="text-2xl font-headline font-bold text-error tracking-tighter">—</p>
      </div>
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Elapsed</p>
        <p id="stat-elapsed" class="text-2xl font-headline font-bold text-on-surface tracking-tighter">—</p>
      </div>
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Rate</p>
        <p id="stat-rate" class="text-2xl font-headline font-bold text-primary tracking-tighter">—<span class="text-xs ml-1 font-normal opacity-40">p/s</span></p>
      </div>
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Skipped</p>
        <p id="stat-skipped" class="text-2xl font-headline font-bold text-on-surface tracking-tighter">—</p>
      </div>
      <div class="space-y-1">
        <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Terms</p>
        <p id="stat-terms" class="text-2xl font-headline font-bold text-on-surface tracking-tighter">—</p>
      </div>
    </div>
  </section>
</div>

<!-- Row 2: SEARCH -->
<section class="space-y-8">
  <div class="flex flex-col md:flex-row md:items-center justify-between gap-6">
    <h3 class="font-headline font-bold text-3xl text-on-surface tracking-tight">Index Search</h3>
    <div class="flex items-center gap-3 flex-1 max-w-3xl">
      <div class="relative flex-1">
        <span class="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-on-surface-variant/40 text-xl">search</span>
        <input id="q" type="text" onkeydown="if(event.key==='Enter') doSearch()"
               class="w-full bg-surface-container-low border border-outline-variant/20 rounded-lg py-4 pl-12 pr-4 focus:ring-1 focus:ring-primary/60 text-sm"
               placeholder="Query nodes, metadata, or document vectors..."/>
      </div>
      <button onclick="doSearch()"
              class="bg-primary-container text-on-primary-container px-8 py-4 rounded-lg font-headline font-bold tracking-tight hover:brightness-105 transition-all">
        SEARCH
      </button>
    </div>
  </div>

  <!-- Search summary -->
  <p id="search-summary" class="text-[10px] font-label text-on-surface-variant uppercase tracking-widest hidden"></p>

  <!-- Results -->
  <div id="results" class="space-y-4"></div>

  <!-- Load more -->
  <div id="load-more-wrap" class="hidden flex justify-center py-12">
    <button id="load-more" onclick="loadMore()"
            class="flex items-center gap-2 text-[10px] font-label uppercase tracking-widest text-on-surface-variant hover:text-primary transition-colors group">
      Fetch Next Result Batch
      <span class="material-symbols-outlined text-sm transition-transform group-hover:translate-y-0.5">keyboard_double_arrow_down</span>
    </button>
  </div>
</section>

</div><!-- /max-w -->
</main>

<!-- Background gradients -->
<div class="fixed top-0 right-0 w-[600px] h-[600px] bg-primary/5 blur-[180px] -z-10 rounded-full"></div>
<div class="fixed bottom-0 left-0 w-[600px] h-[600px] bg-secondary/5 blur-[180px] -z-10 rounded-full"></div>

<!-- Toast container -->
<div id="toast-area" class="fixed bottom-6 right-6 space-y-2 z-50"></div>

<script>
/* ── Helpers ──────────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);

function fmt(n) {
  if (n === undefined || n === null) return '—';
  if (n >= 1000) return (n/1000).toFixed(1) + 'k';
  return String(n);
}

function fmtTime(s) {
  if (!s) return '—';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function showToast(msg, isError=false) {
  const t = document.createElement('div');
  t.className = `px-4 py-3 rounded-lg text-sm font-label border text-on-surface
    ${isError ? 'bg-error/10 border-error/30 text-error' : 'bg-surface-container-high border-outline-variant/20'}`;
  t.textContent = msg;
  $('toast-area').appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

/* ── Crawl control ────────────────────────────────────────────────────── */
async function startCrawl() {
  const isResume = $('resume').checked;
  const url = $('url-input').value.trim();
  if (!isResume && !url) { showToast('Enter a URL', true); return; }
  try {
    const r = await fetch('/crawl', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        url:         isResume ? '' : url,
        depth:       +$('depth').value,
        same_domain: $('sd').checked,
        resume:      isResume
      })
    });
    const d = await r.json();
    showToast(d.message || d.error, !!d.error);
    tick();
  } catch(e) { showToast('Connection error', true); }
}

async function stopCrawl() {
  try {
    const d = await (await fetch('/stop', {method:'POST'})).json();
    showToast(d.message);
    tick();
  } catch(e) { showToast('Connection error', true); }
}

/* ── Resume UI ────────────────────────────────────────────────────────── */
function onResumeToggle() {
  const checked = $('resume').checked;
  $('url-input').disabled  = checked;
  $('depth').disabled      = checked;
  $('sd').disabled         = checked;
  const note = $('resume-note');
  if (checked) {
    fetch('/status').then(r=>r.json()).then(d => {
      if (d.origin_url) {
        $('url-input').value = d.origin_url;
        note.textContent = 'Will resume: ' + d.origin_url;
        note.classList.remove('hidden');
      } else {
        note.textContent = 'No previous crawl found — will start fresh';
        note.classList.remove('hidden');
      }
    }).catch(()=>{});
  } else {
    note.classList.add('hidden');
    $('url-input').disabled = false;
  }
}

/* ── Search ───────────────────────────────────────────────────────────── */
let _searchOffset = 0;
let _lastQuery    = '';
const PAGE_SIZE   = 10;

async function doSearch() {
  const q = $('q').value.trim();
  if (!q) return;
  _lastQuery    = q;
  _searchOffset = 0;
  $('results').innerHTML = '';
  await _fetchResults(q, 0, true);
}

async function loadMore() {
  _searchOffset += PAGE_SIZE;
  await _fetchResults(_lastQuery, _searchOffset, false);
}

async function _fetchResults(q, offset, reset) {
  try {
    const r = await fetch(`/search?q=${encodeURIComponent(q)}&limit=${PAGE_SIZE + offset}`);
    const d = await r.json();
    const summary = $('search-summary');
    const wrap    = $('load-more-wrap');

    if (!d.results || !d.results.length) {
      if (reset) {
        summary.textContent = `No results for "${q}"`;
        summary.classList.remove('hidden');
        $('results').innerHTML = '';
      }
      wrap.classList.add('hidden');
      return;
    }

    const slice = d.results.slice(offset, offset + PAGE_SIZE);

    summary.textContent = `${d.count} result(s) for "${q}" — ${d.elapsed_ms}ms`;
    summary.classList.remove('hidden');

    if (reset) $('results').innerHTML = '';

    slice.forEach((res, i) => {
      const isTop = offset === 0 && i === 0;
      const card  = document.createElement('div');
      card.className = `group bg-surface-container-low hover:bg-surface-container
        border-l-2 ${isTop ? 'border-primary' : 'border-outline-variant/30'}
        p-6 transition-all flex flex-col md:flex-row gap-6
        border border-outline-variant/10`;
      card.innerHTML = `
        <div class="flex-1 space-y-3">
          <div class="flex items-center gap-3">
            <span class="bg-${isTop?'primary/10 text-primary':'surface-container-highest text-on-surface-variant'}
              text-[10px] font-bold px-2 py-0.5 rounded-sm font-label tracking-widest">
              DOC_ID: ${res.page_id || '—'}
            </span>
            <h4 class="text-lg font-headline font-bold text-on-surface group-hover:text-primary transition-colors">
              ${esc(res.title || res.url)}
            </h4>
          </div>
          <p class="text-sm text-on-surface-variant leading-relaxed max-w-4xl">${esc(res.snippet)}</p>
          <div class="flex items-center gap-4 pt-2">
            <a class="text-xs text-primary underline underline-offset-4 decoration-primary/30 hover:decoration-primary font-label"
               href="${esc(res.url)}" target="_blank" rel="noopener">${esc(res.url)}</a>
          </div>
        </div>
        <div class="flex md:flex-col justify-between items-end md:w-32 border-l border-outline-variant/10 pl-6 gap-4">
          <div class="text-right">
            <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Score</p>
            <p class="text-2xl font-headline font-bold ${isTop?'text-primary':'text-on-surface'}">${res.score.toFixed(3)}</p>
          </div>
          <div class="text-right">
            <p class="text-[9px] font-label text-on-surface-variant uppercase tracking-widest">Depth</p>
            <p class="text-lg font-headline font-bold text-on-surface">Lv. ${res.depth}</p>
          </div>
        </div>`;
      $('results').appendChild(card);
    });

    // Show load-more if there are more results beyond what we've shown
    wrap.classList.toggle('hidden', offset + PAGE_SIZE >= d.count);
  } catch(e) { showToast('Search failed', true); }
}

/* ── Status polling ───────────────────────────────────────────────────── */
let _wasRunning = false;

async function tick() {
  try {
    const d = await (await fetch('/status')).json();
    const running = d.is_running;

    // State indicator
    $('state-text').textContent  = running ? 'RUNNING' : 'IDLE';
    $('state-text').className    = `font-headline font-bold ${running ? 'text-primary' : 'text-on-surface-variant'}`;
    $('state-dot').className     = `w-2 h-2 rounded-full transition-all ${running ? 'bg-primary glow-primary' : 'bg-outline-variant'}`;
    $('engine-badge').textContent = running ? 'ACTIVE' : 'READY';
    $('engine-badge').className  = `text-[10px] font-label px-2 py-1 rounded
      ${running ? 'bg-primary/20 text-primary' : 'bg-primary/10 text-primary'}`;

    // Origin
    $('origin-bar').textContent = d.origin_url || '';

    // Stats
    $('stat-fetched').textContent  = fmt(d.pages_fetched);
    $('stat-indexed').textContent  = fmt(d.pages_indexed);
    $('stat-queued').textContent   = fmt(d.pages_queued);
    $('stat-failed').textContent   = fmt(d.pages_failed);
    $('stat-skipped').textContent  = fmt(d.pages_skipped);
    $('stat-terms').textContent    = fmt(d.index_terms);
    $('stat-elapsed').textContent  = fmtTime(d.elapsed_seconds);

    const rate = d.elapsed_seconds > 0
      ? (d.pages_fetched / d.elapsed_seconds).toFixed(2)
      : '0.00';
    $('stat-rate').innerHTML = `${rate}<span class="text-xs ml-1 font-normal opacity-40">p/s</span>`;

    // Progress bar
    const total = (d.pages_fetched + d.pages_queued) || 1;
    const pct   = Math.min(100, (d.pages_fetched / total * 100)).toFixed(1);
    $('pbar').style.width    = pct + '%';
    $('pbar-pct').textContent = pct + '%';

    // Adaptive polling: fast while running, slow when idle
    if (_wasRunning && !running) {
      clearInterval(_pollTimer);
      _pollTimer = setInterval(tick, 10000);
    } else if (!_wasRunning && running) {
      clearInterval(_pollTimer);
      _pollTimer = setInterval(tick, 1800);
    }
    _wasRunning = running;
  } catch(e) {}
}

let _pollTimer = setInterval(tick, 10000);
tick();
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

        # Prepend https:// if the user omitted the scheme (normalize_url handles
        # this too for links found during crawling, but we also do it here so
        # the corrected URL is echoed back in the response message).
        if not resume and url and not url.startswith(("http://", "https://")):
            url = "https://" + url

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