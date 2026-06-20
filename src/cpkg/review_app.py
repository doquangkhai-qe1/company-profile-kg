"""Data-review web UI — inspect every layer the pipeline produces, per ticker/run.

A zero-dependency local viewer (stdlib http.server) that surfaces the four data
layers of the flow side by side:

  1. Crawl    → data/raw/<ticker>/<date>/*.html  (raw HTML + stripped text)
  2. Extract  → data/raw/<ticker>/<date>/extraction.json  (validated Extraction)
  3. Tier-1   → Neo4j deterministic snapshot (Company/Figure/Subsidiary/…)
  4. Tier-2   → Graphiti episodes + extracted entities + temporal facts

Filesystem layers work offline; the Neo4j-backed layers degrade gracefully to an
error payload when the DB is unreachable (same posture as the MCP server). Read-only.

Run:  PYTHONPATH=src python -m cpkg.review_app          # http://127.0.0.1:8765
      PYTHONPATH=src python -m cpkg.review_app --port 9000 --host 0.0.0.0
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import find_company, load_config, load_tickers

# Validation patterns — defend the filesystem reads against traversal.
_TICKER_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_]+\.html$")

# Lazily-created Neo4j driver, shared across requests (guarded by a lock).
_driver = None
_driver_lock = threading.Lock()
_cfg = None


def cfg():
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


# ── neo4j helpers (degrade-safe) ─────────────────────────────────────────────

def _driver_get():
    """Return a live driver or raise. Reconnects if connectivity dropped."""
    global _driver
    from . import kgtier1
    with _driver_lock:
        if _driver is None:
            _driver = kgtier1.connect(cfg())
        else:
            try:
                _driver.verify_connectivity()
            except Exception:
                try:
                    _driver.close()
                except Exception:
                    pass
                _driver = kgtier1.connect(cfg())
        return _driver


def _jsonable(v):
    """Coerce neo4j temporal types (and the like) to JSON-friendly values."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


# ── data access: filesystem layers ───────────────────────────────────────────

def _data_root() -> Path:
    return cfg().data_dir


def list_tickers() -> list[dict]:
    """Every ticker that has crawled data on disk, enriched with config name + runs."""
    root = _data_root()
    out: list[dict] = []
    seen = set()
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir() or not _TICKER_RE.match(d.name):
                continue
            runs = sorted([p.name for p in d.iterdir()
                           if p.is_dir() and _DATE_RE.match(p.name)], reverse=True)
            comp = find_company(d.name) or {}
            out.append({"ticker": d.name, "name": comp.get("name", ""),
                        "runs": runs, "run_count": len(runs)})
            seen.add(d.name.upper())
    # surface configured tickers that have no data yet (greyed out in the UI)
    for comp in load_tickers():
        t = str(comp.get("ticker", "")).upper()
        if t and t not in seen:
            out.append({"ticker": t, "name": comp.get("name", ""),
                        "runs": [], "run_count": 0})
    out.sort(key=lambda r: (r["run_count"] == 0, r["ticker"]))
    return out


def _run_dir(ticker: str, day: str) -> Path | None:
    if not (_TICKER_RE.match(ticker) and _DATE_RE.match(day)):
        return None
    p = _data_root() / ticker.upper() / day
    return p if p.is_dir() else None


def list_crawl(ticker: str, day: str) -> dict:
    d = _run_dir(ticker, day)
    if not d:
        return {"ok": False, "error": "run not found"}
    pages = []
    for f in sorted(d.glob("*.html")):
        src, _, kind = f.stem.partition("_")
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            pages.append({"file": f.name, "error": str(e)})
            continue
        pages.append({
            "file": f.name, "source": src, "kind": kind or src,
            "bytes": f.stat().st_size, "raw_chars": len(raw),
        })
    return {"ok": True, "ticker": ticker.upper(), "date": day, "pages": pages}


def crawl_page(ticker: str, day: str, file: str) -> dict:
    d = _run_dir(ticker, day)
    if not d or not _FILE_RE.match(file):
        return {"ok": False, "error": "page not found"}
    f = d / file
    if not f.is_file():
        return {"ok": False, "error": "page not found"}
    from .crawl import _html_to_text
    raw = f.read_text(encoding="utf-8", errors="replace")
    src, _, kind = f.stem.partition("_")
    return {"ok": True, "file": file, "source": src, "kind": kind or src,
            "bytes": f.stat().st_size, "text": _html_to_text(raw), "raw": raw}


def extraction(ticker: str, day: str) -> dict:
    d = _run_dir(ticker, day)
    if not d:
        return {"ok": False, "error": "run not found"}
    f = d / "extraction.json"
    if not f.is_file():
        return {"ok": False, "error": "no extraction.json for this run"}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"parse failed: {e}"}
    return {"ok": True, "data": data}


# ── data access: Neo4j layers ────────────────────────────────────────────────

def tier1(ticker: str) -> dict:
    from . import kgtier1
    t = ticker.upper()
    db = cfg().neo4j_database
    try:
        drv = _driver_get()
        company = kgtier1._company_node(drv, db, t)
        if not company:
            return {"ok": True, "ticker": t, "present": False,
                    "note": "no Company node — Tier-1 ingest hasn't run for this ticker"}
        runs, _, _ = drv.execute_query(
            "MATCH (:Company {ticker:$t})-[:HAS_RUN]->(r:CrawlRun) "
            "RETURN r.crawl_date AS crawl_date, r.as_of AS as_of ORDER BY r.crawl_date DESC",
            t=t, database_=db)
        return {
            "ok": True, "ticker": t, "present": True,
            "company": _jsonable(company),
            "runs": [_jsonable(dict(r)) for r in runs],
            "figures": _jsonable(kgtier1.get_figures(drv, db, t)),
            "subsidiaries": _jsonable(kgtier1.get_subsidiaries(drv, db, t)),
            "ownership": _jsonable(kgtier1.get_ownership(drv, db, t)),
            "board": _jsonable(kgtier1.get_board(drv, db, t)),
            "dividends": _jsonable(kgtier1.get_dividends(drv, db, t)),
            "insider_deals": _jsonable(kgtier1.get_insider_deals(drv, db, t)),
            "audit_firm": kgtier1.get_audit_firm(drv, db, t),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tier2(ticker: str) -> dict:
    """Graphiti layer for a ticker: episodes (by name prefix) + their entities + facts.

    Queried straight over Cypher (no LLM/embedder needed) so it works even when
    Ollama is down. Episode names are `{ticker}.{section}@{date}` (see build_episodes).
    """
    t = ticker.upper()
    db = cfg().neo4j_database
    gid = cfg().group_id
    prefix = f"{t}."
    try:
        drv = _driver_get()
        eps, _, _ = drv.execute_query(
            "MATCH (e:Episodic) WHERE e.group_id=$gid AND e.name STARTS WITH $prefix "
            "RETURN e.uuid AS uuid, e.name AS name, e.content AS content, "
            "       e.created_at AS created_at, e.valid_at AS valid_at, "
            "       e.source_description AS source_description ORDER BY e.name",
            gid=gid, prefix=prefix, database_=db)
        episodes = [_jsonable(dict(r)) for r in eps]
        ents, _, _ = drv.execute_query(
            "MATCH (e:Episodic) WHERE e.group_id=$gid AND e.name STARTS WITH $prefix "
            "MATCH (e)-[:MENTIONS]->(n:Entity) "
            "RETURN DISTINCT n.name AS name, n.summary AS summary, "
            "       [l IN labels(n) WHERE l<>'Entity'] AS types ORDER BY n.name",
            gid=gid, prefix=prefix, database_=db)
        entities = [_jsonable(dict(r)) for r in ents]
        facts, _, _ = drv.execute_query(
            "MATCH (e:Episodic) WHERE e.group_id=$gid AND e.name STARTS WITH $prefix "
            "WITH collect(DISTINCT e.uuid) AS euids "
            "MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) "
            "WHERE r.episodes IS NOT NULL AND any(x IN r.episodes WHERE x IN euids) "
            "RETURN DISTINCT r.fact AS fact, a.name AS src, b.name AS dst, "
            "       r.valid_at AS valid_at, r.invalid_at AS invalid_at ORDER BY r.fact",
            gid=gid, prefix=prefix, database_=db)
        facts_out = []
        for r in facts:
            d = _jsonable(dict(r))
            d["status"] = "superseded" if d.get("invalid_at") else "current"
            facts_out.append(d)
        return {"ok": True, "ticker": t, "group_id": gid,
                "episodes": episodes, "entities": entities, "facts": facts_out}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def status() -> dict:
    c = cfg()
    neo = {"configured_uri": c.neo4j_uri, "database": c.neo4j_database}
    try:
        _driver_get()
        neo["ok"] = True
    except Exception as e:  # noqa: BLE001
        neo["ok"] = False
        neo["error"] = f"{type(e).__name__}: {e}"
    return {"ok": True, "data_dir": str(c.data_dir), "neo4j": neo,
            "group_id": c.group_id, "tickers_with_data":
                sum(1 for r in list_tickers() if r["run_count"])}


# ── HTTP routing ─────────────────────────────────────────────────────────────

ROUTES = {
    "/api/status": lambda q: status(),
    "/api/tickers": lambda q: {"ok": True, "tickers": list_tickers()},
    "/api/crawl": lambda q: list_crawl(_one(q, "ticker"), _one(q, "date")),
    "/api/crawl/page": lambda q: crawl_page(_one(q, "ticker"), _one(q, "date"), _one(q, "file")),
    "/api/extraction": lambda q: extraction(_one(q, "ticker"), _one(q, "date")),
    "/api/tier1": lambda q: tier1(_one(q, "ticker")),
    "/api/tier2": lambda q: tier2(_one(q, "ticker")),
}


def _one(q: dict, key: str) -> str:
    v = q.get(key)
    return v[0] if v else ""


class Handler(BaseHTTPRequestHandler):
    server_version = "cpkg-review/1.0"

    def log_message(self, *a):  # quieter console
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        fn = ROUTES.get(path)
        if not fn:
            self._send(404, b'{"ok":false,"error":"not found"}',
                       "application/json; charset=utf-8")
            return
        try:
            payload = fn(parse_qs(parts.query))
        except Exception as e:  # noqa: BLE001
            payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")


def main(argv=None):
    import os
    ap = argparse.ArgumentParser(description="company-profile-kg data-review UI")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = ap.parse_args(argv)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"company-profile-kg review UI → {url}  (Ctrl-C to stop)")
    print(f"  data dir: {cfg().data_dir}")
    print(f"  neo4j:    {cfg().neo4j_uri}  (Tier-1/Tier-2 degrade-safe if unreachable)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()


# ── single-page UI (vanilla, no build step) ──────────────────────────────────

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>company-profile-kg · Data Review</title>
<style>
  :root{
    --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --border:#2a2f3a;
    --fg:#e6e9ef; --muted:#8b93a3; --accent:#5b8cff; --accent2:#36c2a6;
    --warn:#e0a458; --bad:#e0596b; --good:#36c2a6; --chip:#262b36;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  a{color:var(--accent);text-decoration:none}
  .app{display:flex;flex-direction:column;height:100vh}
  header{display:flex;align-items:center;gap:14px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel)}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
  header .sub{color:var(--muted);font-size:12px}
  .grow{flex:1}
  select{background:var(--panel2);color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:6px 10px;font-size:13px;outline:none}
  .pill{font-size:12px;padding:3px 9px;border-radius:999px;background:var(--chip);color:var(--muted);border:1px solid var(--border)}
  .pill.ok{color:var(--good);border-color:#26463e}
  .pill.bad{color:var(--bad);border-color:#492a31}
  .stages{display:flex;gap:6px;padding:8px 16px;border-bottom:1px solid var(--border);background:var(--panel);overflow-x:auto}
  .stage{display:flex;align-items:center;gap:8px;padding:7px 13px;border-radius:9px;cursor:pointer;border:1px solid transparent;color:var(--muted);white-space:nowrap}
  .stage:hover{background:var(--panel2)}
  .stage.active{background:var(--panel2);border-color:var(--border);color:var(--fg)}
  .stage .n{font-size:11px;background:var(--chip);border-radius:6px;padding:1px 7px;color:var(--muted)}
  .stage.active .n{background:var(--accent);color:#0b0d12}
  .stage .ico{font-size:15px}
  main{flex:1;display:flex;min-height:0}
  .list{width:300px;min-width:240px;border-right:1px solid var(--border);overflow:auto;background:var(--panel)}
  .detail{flex:1;overflow:auto;padding:0}
  .item{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer}
  .item:hover{background:var(--panel2)}
  .item.active{background:var(--panel2);box-shadow:inset 3px 0 0 var(--accent)}
  .item .t{font-weight:600}
  .item .m{color:var(--muted);font-size:12px;margin-top:2px}
  .group-h{padding:8px 14px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.7px;background:var(--bg);position:sticky;top:0;border-bottom:1px solid var(--border)}
  .pad{padding:18px 22px}
  .empty{color:var(--muted);padding:40px 22px;text-align:center}
  .crumbs{color:var(--muted);font-size:12px;padding:10px 22px;border-bottom:1px solid var(--border);background:var(--panel)}
  h2.sec{font-size:14px;margin:22px 0 8px;color:var(--accent2);border-bottom:1px solid var(--border);padding-bottom:6px}
  h2.sec:first-child{margin-top:0}
  table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0 14px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--border);vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  tr:hover td{background:var(--panel2)}
  .kv{display:grid;grid-template-columns:180px 1fr;gap:2px 14px;margin:6px 0 14px}
  .kv .k{color:var(--muted)}
  .tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;background:var(--chip);color:var(--muted);margin-right:4px;border:1px solid var(--border)}
  .tag.green{color:var(--good);border-color:#26463e}
  .tag.warn{color:var(--warn);border-color:#4a3a25}
  .tag.blue{color:var(--accent);border-color:#26344e}
  pre{background:var(--panel2);border:1px solid var(--border);border-radius:9px;padding:14px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
  .text-body{background:var(--panel2);border:1px solid var(--border);border-radius:9px;padding:16px;white-space:pre-wrap;max-height:none;font-size:13px}
  .toolbar{display:flex;gap:8px;align-items:center;padding:10px 22px;border-bottom:1px solid var(--border);background:var(--panel)}
  button.btn{background:var(--panel2);color:var(--fg);border:1px solid var(--border);border-radius:8px;padding:5px 12px;cursor:pointer;font-size:12px}
  button.btn:hover{border-color:var(--accent)}
  button.btn.on{background:var(--accent);color:#0b0d12;border-color:var(--accent)}
  .count{color:var(--muted);font-size:12px;margin-left:auto}
  .muted{color:var(--muted)}
  .nowrap{white-space:nowrap}
  .err{color:var(--bad);padding:18px 22px}
  .badge{font-size:11px;padding:1px 7px;border-radius:999px}
  .badge.current{background:#11362e;color:var(--good)}
  .badge.superseded{background:#3a2a30;color:var(--warn)}
  .subnav{display:flex;gap:6px;padding:10px 22px 0}
  .subnav .s{padding:5px 11px;border-radius:8px 8px 0 0;cursor:pointer;color:var(--muted);border:1px solid transparent;border-bottom:none}
  .subnav .s.active{background:var(--panel2);color:var(--fg);border-color:var(--border)}
  .scroll{overflow:auto}
  .flow{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;margin-left:6px}
  .flow b{color:var(--fg)}
  .dot{width:6px;height:6px;border-radius:50%;background:var(--border)}
  .dot.has{background:var(--accent2)}
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>🏦 company-profile-kg</h1>
    <span class="sub">Data Review</span>
    <select id="tickerSel" title="Mã ngân hàng"></select>
    <select id="dateSel" title="Ngày crawl (run)"></select>
    <div class="flow" id="flow"></div>
    <div class="grow"></div>
    <span class="pill" id="neoPill">Neo4j …</span>
  </header>
  <div class="stages" id="stages"></div>
  <main>
    <div class="list" id="list"></div>
    <div class="detail" id="detail"></div>
  </main>
</div>
<script>
const STAGES = [
  {id:"crawl",    ico:"🕸️", label:"Crawl",   need:"date", desc:"HTML thô + text bóc tách"},
  {id:"extract",  ico:"🧩", label:"Extract", need:"date", desc:"extraction.json (Claude)"},
  {id:"tier1",    ico:"🗃️", label:"Tier-1",  need:"ticker", desc:"Neo4j snapshot"},
  {id:"tier2",    ico:"🧠", label:"Tier-2",  need:"ticker", desc:"Graphiti episodes/facts"},
];
const S = {ticker:null, date:null, stage:"crawl", tickers:[], cache:{}, sel:{}, sub:{}, raw:{}};
const $ = s => document.querySelector(s);
const el = (t,c,h)=>{const e=document.createElement(t); if(c)e.className=c; if(h!=null)e.innerHTML=h; return e;};
const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));
const num = n => (n==null||n==="")?'<span class="muted">—</span>':esc(n);
async function api(p){ const r=await fetch(p); return r.json(); }

function setFlow(){
  const f=$("#flow"); if(!S.ticker){f.innerHTML="";return;}
  const t=S.tickers.find(x=>x.ticker===S.ticker)||{};
  f.innerHTML = `<b>${esc(S.ticker)}</b> ${t.name?('· '+esc(t.name)):''} · ${t.run_count||0} run`;
}

async function loadTickers(){
  const r = await api("/api/tickers");
  S.tickers = r.tickers||[];
  const sel=$("#tickerSel"); sel.innerHTML="";
  for(const t of S.tickers){
    const o=el("option"); o.value=t.ticker;
    o.textContent = t.ticker + (t.run_count?` (${t.run_count})`:" · no data");
    if(!t.run_count) o.style.color="#666";
    sel.appendChild(o);
  }
  const first = S.tickers.find(t=>t.run_count) || S.tickers[0];
  if(first){ sel.value=first.ticker; S.ticker=first.ticker; }
  fillDates();
}
function fillDates(){
  const t=S.tickers.find(x=>x.ticker===S.ticker);
  const sel=$("#dateSel"); sel.innerHTML="";
  const runs=(t&&t.runs)||[];
  for(const d of runs){ const o=el("option"); o.value=d; o.textContent=d; sel.appendChild(o); }
  if(!runs.length){ const o=el("option"); o.value=""; o.textContent="— chưa có run —"; sel.appendChild(o); }
  S.date = runs[0]||null;
  sel.value = S.date||"";
  setFlow();
}

async function loadStatus(){
  const r = await api("/api/status");
  const p=$("#neoPill"); const neo=r.neo4j||{};
  if(neo.ok){ p.className="pill ok"; p.textContent="Neo4j ✓"; p.title=neo.configured_uri; }
  else{ p.className="pill bad"; p.textContent="Neo4j ✕"; p.title=(neo.error||"")+" @ "+(neo.configured_uri||""); }
}

function renderStages(){
  const c=$("#stages"); c.innerHTML="";
  for(const st of STAGES){
    const d=el("div","stage"+(st.stage===S.stage?"":""));
    d.className="stage"+(st.id===S.stage?" active":"");
    d.innerHTML=`<span class="ico">${st.ico}</span><span>${st.label}</span><span class="n" id="n_${st.id}"></span>`;
    d.title=st.desc;
    d.onclick=()=>{S.stage=st.id; renderStages(); load();};
    c.appendChild(d);
  }
}
function setCount(stage,n){ const e=$("#n_"+stage); if(e) e.textContent=(n==null?"":n); }

// ---------- generic master/detail ----------
function showList(html){ $("#list").innerHTML=html; }
function showDetail(html){ $("#detail").innerHTML=html; }
function selectItem(key){ S.sel[S.stage]=key; document.querySelectorAll("#list .item").forEach(n=>n.classList.toggle("active", n.dataset.key===key)); }

async function load(){
  const st=STAGES.find(s=>s.id===S.stage);
  if(st.need==="date" && !S.date){ showList(""); showDetail('<div class="empty">Mã này chưa có run nào trên đĩa.</div>'); return; }
  if(S.stage==="crawl") return loadCrawl();
  if(S.stage==="extract") return loadExtract();
  if(S.stage==="tier1") return loadTier1();
  if(S.stage==="tier2") return loadTier2();
}

// ---------- CRAWL ----------
async function loadCrawl(){
  showDetail('<div class="empty">Đang tải…</div>');
  const r = await api(`/api/crawl?ticker=${S.ticker}&date=${S.date}`);
  if(!r.ok){ showList(""); showDetail(`<div class="err">${esc(r.error)}</div>`); setCount("crawl",0); return; }
  setCount("crawl", r.pages.length);
  let html=`<div class="group-h">${r.pages.length} trang đã crawl · ${esc(S.date)}</div>`;
  r.pages.forEach((p,i)=>{
    html += `<div class="item" data-key="${esc(p.file)}" onclick="openCrawl('${esc(p.file)}')">
      <div class="t">${esc(p.source)} · ${esc(p.kind)}</div>
      <div class="m">${esc(p.file)} · ${fmtBytes(p.bytes)} · ${(p.raw_chars||0).toLocaleString()} ký tự HTML</div></div>`;
  });
  showList(html);
  if(r.pages[0]) openCrawl(r.pages[0].file);
}
function fmtBytes(b){ if(b==null)return"—"; if(b<1024)return b+" B"; if(b<1048576)return (b/1024).toFixed(0)+" KB"; return (b/1048576).toFixed(1)+" MB"; }
async function openCrawl(file){
  selectItem(file);
  showDetail('<div class="empty">Đang tải…</div>');
  const r = await api(`/api/crawl/page?ticker=${S.ticker}&date=${S.date}&file=${encodeURIComponent(file)}`);
  if(!r.ok){ showDetail(`<div class="err">${esc(r.error)}</div>`); return; }
  S.raw.crawl = r;
  const mode = S.sub.crawl || "text";
  const body = mode==="raw" ? r.raw : r.text;
  showDetail(`
    <div class="toolbar">
      <strong>${esc(r.source)} · ${esc(r.kind)}</strong>
      <button class="btn ${mode==='text'?'on':''}" onclick="setCrawlMode('text')">Text bóc tách</button>
      <button class="btn ${mode==='raw'?'on':''}" onclick="setCrawlMode('raw')">HTML thô</button>
      <span class="count">${(body||'').length.toLocaleString()} ký tự</span>
    </div>
    <div class="pad"><div class="text-body">${esc(body)||'<span class="muted">trống</span>'}</div></div>`);
}
function setCrawlMode(m){ S.sub.crawl=m; const r=S.raw.crawl; if(r) openCrawl(r.file); }

// ---------- EXTRACT ----------
const EXTRACT_SECTIONS = [
  ["identity","Định danh"],["profile","Hồ sơ (profile)"],["organisation","Tổ chức & công ty con"],
  ["strategy","Định hướng"],["capital","Kế hoạch vốn"],["esg","ESG"],
  ["ownership","Sở hữu / cổ đông"],["board","HĐQT/BĐH/BKS"],["dividends","Cổ tức"],
  ["insider","Giao dịch nội bộ"],["figures","Chỉ số (figures)"],["needs_confirm","Cần xác minh"],
  ["sources","Nguồn"],["raw","JSON thô"]
];
async function loadExtract(){
  showDetail('<div class="empty">Đang tải…</div>');
  const r = await api(`/api/extraction?ticker=${S.ticker}&date=${S.date}`);
  if(!r.ok){ showList(""); showDetail(`<div class="err">${esc(r.error)}</div>`); setCount("extract",""); return; }
  S.raw.extract = r.data;
  const counts = extractCounts(r.data);
  setCount("extract", counts._total);
  let html=`<div class="group-h">extraction.json · ${esc(S.date)}</div>`;
  for(const [id,label] of EXTRACT_SECTIONS){
    const c = counts[id];
    const badge = (c==null)?"":`<span class="m">${c}</span>`;
    html += `<div class="item" data-key="${id}" onclick="openExtract('${id}')">
      <div class="t">${esc(label)}</div>${badge?('<div class="m">'+c+' mục</div>'):''}</div>`;
  }
  showList(html);
  openExtract(S.sel.extract && counts.hasOwnProperty(S.sel.extract)?S.sel.extract:"identity");
}
function extractCounts(d){
  const g=d.governance||{}, p=d.profile||{}, o=(p.organisation||{});
  return {
    _total: Object.keys(d.figures||{}).length + (g.ownership||[]).length + (g.board||[]).length,
    organisation:(o.subsidiaries||[]).length, ownership:(g.ownership||[]).length,
    board:(g.board||[]).length, dividends:(g.dividend_history||[]).length,
    insider:(g.insider_dealing||[]).length, figures:Object.keys(d.figures||{}).length,
    needs_confirm:(d.needs_confirm||[]).length, sources:(d.sources||[]).length,
  };
}
function openExtract(id){
  selectItem(id);
  const d=S.raw.extract||{}; const p=d.profile||{}, g=d.governance||{};
  let h="";
  if(id==="raw"){ showDetail(`<div class="pad"><pre>${esc(JSON.stringify(d,null,2))}</pre></div>`); return; }
  if(id==="identity"){
    h=kv({"Ticker":d.ticker,"As of":d.as_of});
  } else if(id==="profile"){
    h=section("Lịch sử", longText(p.history_vi))+section("Mô hình kinh doanh", longText(p.business_model_vi));
  } else if(id==="organisation"){
    const o=p.organisation||{};
    h=kv({"Cơ cấu":o.structure_vi,"Nhân viên":o.employees,"Chi nhánh":o.branches,"PGD":o.pgd,"ATM":o.atm,"Tỉnh/TP":o.provinces});
    h+=tableOf(o.subsidiaries||[], ["name","stake_pct","line_vi","source"], ["Tên","% sở hữu","Ngành","Nguồn"], "Công ty con / liên kết");
  } else if(id==="strategy"){
    const s=p.strategy_direction||{};
    h=kv({"Tầm nhìn":s.vision_vi,"claim_type":s.claim_type,"Nguồn":s.source});
    const tg=s.targets||{}; if(Object.keys(tg).length){ h+='<h2 class="sec">Mục tiêu</h2>'+kv(tg); }
  } else if(id==="capital"){
    const c=p.capital_plan||{};
    h=kv({"Vốn điều lệ (str)":c.charter_capital_str,"Vốn điều lệ (num)":c.charter_capital_num,"Nguồn":c.source});
    h+=tableOf(c.components||[],["type","size_vi","partner_vi","status_vi","source"],["Loại","Quy mô","Đối tác","Trạng thái","Nguồn"],"Cấu phần tăng vốn");
  } else if(id==="esg"){
    const e=p.esg_facts||{}; h=kv({"Tín dụng xanh":e.green_credit_vi,"Khung PTBV":e.framework_vi,"Xã hội":e.social_vi,"Nguồn":e.source});
  } else if(id==="ownership"){
    h=kv({"Room ngoại":g.foreign_room_vi,"Đối tác chiến lược":g.strategic_partner_vi});
    h+=tableOf(g.ownership||[],["holder_vi","pct","is_foreign","is_strategic","source"],["Cổ đông","%","NN","Chiến lược","Nguồn"],"Cổ đông lớn");
  } else if(id==="board"){
    h=kv({"Kiểm toán":g.audit_firm});
    h+=tableOf(g.board||[],["name_vi","role_vi","body","independent","source"],["Họ tên","Vai trò","Cơ quan","Độc lập","Nguồn"],"Thành viên HĐQT/BĐH/BKS");
  } else if(id==="dividends"){
    h=tableOf(g.dividend_history||[],["year","type","pct","note_vi","source"],["Năm","Loại","%","Ghi chú","Nguồn"],"Lịch sử cổ tức");
  } else if(id==="insider"){
    h=tableOf(g.insider_dealing||[],["person_vi","relation_vi","side","qty","date","source"],["Người","Quan hệ","Chiều","KL","Ngày","Nguồn"],"Giao dịch nội bộ");
    if(g.related_party_vi) h+=section("Bên liên quan", longText(g.related_party_vi));
  } else if(id==="figures"){
    const rows=Object.entries(d.figures||{}).map(([k,v])=>Object.assign({key:k},v));
    h=tableOf(rows,["key","label_vi","value_num","value_str","unit","scope","confidence","source"],
      ["Key","Nhãn","Số","Chuỗi","ĐV","Phạm vi","Tin cậy","Nguồn"],"Figures");
  } else if(id==="needs_confirm"){
    const arr=d.needs_confirm||[]; h = arr.length? '<ul>'+arr.map(x=>`<li>${esc(x)}</li>`).join('')+'</ul>' : '<div class="muted">— không có —</div>';
    h=`<h2 class="sec">Cần xác minh (${arr.length})</h2>`+h;
  } else if(id==="sources"){
    h=tableOf(d.sources||[],["id","kind","url","retrieved"],["ID","Loại","URL","Ngày lấy"],"Nguồn dữ liệu");
  }
  showDetail(`<div class="pad">${h||'<div class="muted">— trống —</div>'}</div>`);
}

// ---------- TIER-1 ----------
const T1_SECTIONS = [
  ["company","Định danh + run", d=>1],
  ["figures","Figures", d=>(d.figures||[]).length],
  ["subsidiaries","Công ty con", d=>(d.subsidiaries||[]).length],
  ["ownership","Cổ đông", d=>(d.ownership||[]).length],
  ["board","HĐQT/BĐH/BKS", d=>(d.board||[]).length],
  ["dividends","Cổ tức", d=>(d.dividends||[]).length],
  ["insider_deals","Giao dịch nội bộ", d=>(d.insider_deals||[]).length],
];
async function loadTier1(){
  showDetail('<div class="empty">Đang truy vấn Neo4j…</div>'); showList("");
  const r = await api(`/api/tier1?ticker=${S.ticker}`);
  if(!r.ok){ showDetail(`<div class="err">Neo4j: ${esc(r.error)}</div>`); setCount("tier1",""); return; }
  S.raw.tier1=r;
  if(!r.present){ showDetail(`<div class="empty">${esc(r.note||'Chưa ingest Tier-1 cho mã này.')}</div>`); setCount("tier1",0); return; }
  let total=0; T1_SECTIONS.forEach(([id,,f])=>{ if(id!=="company") total+=f(r); });
  setCount("tier1", total);
  let html=`<div class="group-h">Neo4j · ${esc(r.ticker)}</div>`;
  for(const [id,label,f] of T1_SECTIONS){
    const c = id==="company"?"":f(r);
    html+=`<div class="item" data-key="${id}" onclick="openTier1('${id}')"><div class="t">${esc(label)}</div>${id==='company'?'':('<div class="m">'+c+' mục</div>')}</div>`;
  }
  showList(html);
  openTier1(S.sel.tier1||"company");
}
function openTier1(id){
  selectItem(id); const r=S.raw.tier1||{}; let h="";
  if(id==="company"){
    const c=r.company||{};
    h=kv({"Ticker":c.ticker,"Tên":c.name,"Last crawl":c.last_crawl,"Aliases":(c.aliases||[]).join(", ")});
    h+=tableOf(r.runs||[],["crawl_date","as_of"],["Crawl date","As of"],"Lịch sử run (CrawlRun)");
    if(r.audit_firm) h+=kv({"Kiểm toán (AuditFirm)":r.audit_firm});
  } else if(id==="figures"){
    h=tableOf(r.figures||[],["key","label_vi","value_num","value_str","unit","scope","confidence","last_seen","source"],
      ["Key","Nhãn","Số","Chuỗi","ĐV","Phạm vi","Tin cậy","Last seen","Nguồn"],"Figure nodes");
  } else if(id==="subsidiaries"){
    h=tableOf(r.subsidiaries||[],["name","stake_pct","line_vi","last_seen","source"],["Tên","% sở hữu","Ngành","Last seen","Nguồn"],"Subsidiary nodes");
  } else if(id==="ownership"){
    h=tableOf(r.ownership||[],["holder_vi","pct","is_foreign","is_strategic","last_seen","source"],["Cổ đông","%","NN","Chiến lược","Last seen","Nguồn"],"Shareholder nodes");
  } else if(id==="board"){
    h=tableOf(r.board||[],["name_vi","role_vi","body","independent","last_seen","source"],["Họ tên","Vai trò","Cơ quan","Độc lập","Last seen","Nguồn"],"Officer nodes");
  } else if(id==="dividends"){
    h=tableOf(r.dividends||[],["year","type","pct","note_vi","source"],["Năm","Loại","%","Ghi chú","Nguồn"],"Dividend nodes");
  } else if(id==="insider_deals"){
    h=tableOf(r.insider_deals||[],["person_vi","relation_vi","side","qty","date","source"],["Người","Quan hệ","Chiều","KL","Ngày","Nguồn"],"InsiderDeal nodes");
  }
  showDetail(`<div class="pad">${h||'<div class="muted">— trống —</div>'}</div>`);
}

// ---------- TIER-2 ----------
async function loadTier2(){
  showDetail('<div class="empty">Đang truy vấn Graphiti…</div>'); showList("");
  const r = await api(`/api/tier2?ticker=${S.ticker}`);
  if(!r.ok){ showDetail(`<div class="err">Neo4j: ${esc(r.error)}</div>`); setCount("tier2",""); return; }
  S.raw.tier2=r;
  setCount("tier2", (r.episodes||[]).length);
  const sub = S.sub.tier2 || "episodes";
  let html=`<div class="group-h">Graphiti · group_id=${esc(r.group_id)}</div>`;
  html+=`<div class="item ${sub==='episodes'?'active':''}" onclick="setTier2('episodes')"><div class="t">📑 Episodes</div><div class="m">${(r.episodes||[]).length} mục đã ingest</div></div>`;
  html+=`<div class="item ${sub==='facts'?'active':''}" onclick="setTier2('facts')"><div class="t">🔗 Facts (edges)</div><div class="m">${(r.facts||[]).length} quan hệ temporal</div></div>`;
  html+=`<div class="item ${sub==='entities'?'active':''}" onclick="setTier2('entities')"><div class="t">⬡ Entities</div><div class="m">${(r.entities||[]).length} thực thể</div></div>`;
  showList(html);
  renderTier2(sub);
}
function setTier2(sub){ S.sub.tier2=sub; loadTier2(); }
function renderTier2(sub){
  const r=S.raw.tier2||{}; let h="";
  if(sub==="episodes"){
    const eps=r.episodes||[];
    if(!eps.length){ h='<div class="muted">Chưa có episode nào — Tier-2 có thể chưa chạy (cần Ollama/LLM).</div>'; }
    eps.forEach(e=>{
      h+=`<div style="margin-bottom:18px">
        <h2 class="sec">${esc(e.name)}</h2>
        <div class="muted" style="font-size:12px;margin-bottom:6px">created: ${esc(e.created_at||'—')} · valid: ${esc(e.valid_at||'—')}</div>
        <div class="text-body">${esc(e.content)}</div></div>`;
    });
  } else if(sub==="facts"){
    const f=r.facts||[];
    h=`<table><thead><tr><th>Trạng thái</th><th>Fact</th><th>Từ → Đến</th><th class="nowrap">Valid</th><th class="nowrap">Invalid</th></tr></thead><tbody>`;
    if(!f.length) h+=`<tr><td colspan="5" class="muted">— không có fact —</td></tr>`;
    f.forEach(x=>{ h+=`<tr>
      <td><span class="badge ${x.status}">${x.status}</span></td>
      <td>${esc(x.fact)}</td>
      <td class="muted nowrap">${esc(x.src)} → ${esc(x.dst)}</td>
      <td class="muted nowrap">${esc(x.valid_at||'—')}</td>
      <td class="muted nowrap">${esc(x.invalid_at||'—')}</td></tr>`; });
    h+=`</tbody></table>`;
  } else if(sub==="entities"){
    const e=r.entities||[];
    h=`<table><thead><tr><th>Tên</th><th>Loại</th><th>Tóm tắt</th></tr></thead><tbody>`;
    if(!e.length) h+=`<tr><td colspan="3" class="muted">— không có entity —</td></tr>`;
    e.forEach(x=>{ h+=`<tr><td class="nowrap">${esc(x.name)}</td>
      <td>${(x.types||[]).map(t=>`<span class="tag blue">${esc(t)}</span>`).join('')}</td>
      <td>${esc(x.summary)||'<span class="muted">—</span>'}</td></tr>`; });
    h+=`</tbody></table>`;
  }
  showDetail(`<div class="pad">${h}</div>`);
}

// ---------- render helpers ----------
function kv(obj){
  const rows=Object.entries(obj).filter(([k,v])=>v!=null&&v!=="");
  if(!rows.length) return '<div class="muted">— trống —</div>';
  return '<div class="kv">'+rows.map(([k,v])=>`<div class="k">${esc(k)}</div><div>${esc(v)}</div>`).join('')+'</div>';
}
function section(title, body){ return `<h2 class="sec">${esc(title)}</h2>${body}`; }
function longText(t){ return t? `<div class="text-body">${esc(t)}</div>` : '<div class="muted">— trống —</div>'; }
function cell(v){
  if(v===true) return '<span class="tag green">✓</span>';
  if(v===false) return '<span class="tag">✕</span>';
  if(v==null||v==="") return '<span class="muted">—</span>';
  const s=String(v);
  if(/^https?:\/\//.test(s)) return `<a href="${esc(s)}" target="_blank" rel="noopener">${esc(s.length>48?s.slice(0,48)+'…':s)}</a>`;
  return esc(s);
}
function tableOf(rows, keys, heads, title){
  let h = title?`<h2 class="sec">${esc(title)} <span class="muted" style="font-weight:400">(${rows.length})</span></h2>`:"";
  if(!rows||!rows.length) return h+'<div class="muted">— không có dữ liệu —</div>';
  h+='<table><thead><tr>'+heads.map(x=>`<th>${esc(x)}</th>`).join('')+'</tr></thead><tbody>';
  for(const r of rows){ h+='<tr>'+keys.map(k=>`<td>${cell(r[k])}</td>`).join('')+'</tr>'; }
  return h+'</tbody></table>';
}

// ---------- wiring ----------
$("#tickerSel").onchange = e=>{ S.ticker=e.target.value; fillDates(); load(); };
$("#dateSel").onchange = e=>{ S.date=e.target.value||null; load(); };
(async function init(){
  renderStages();
  await Promise.all([loadStatus(), loadTickers()]);
  load();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
