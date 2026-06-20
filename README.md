# company-profile-kg

A **standalone** knowledge-graph service that continuously aggregates Vietnamese-bank
**company profile** and **corporate-actions / governance** facts from public sources
(CafeF, Vietstock, company IR sites), stores them in a **Graphiti temporal knowledge
graph** (Neo4j), and exposes them over **MCP** — queryable by ticker *or* company name.

It is a *prior-knowledge provider* for the `vn-bank-research-unified` research workflow
(it feeds `bank-company-profile-extractor` and `bank-corpactions-gov-extractor`). It does
**not** replace primary sources (masterfile / BCTN / tca); every fact carries a `source`
URL and a `confidence`.

## How it works

```
                 daily (cron/launchd)
 crawl.py  ──►  extract.py  ──►  kgtier1.py (Cypher snapshot)
 (CafeF/        (claude -p,       kggraphiti.py (Graphiti temporal episodes)
  Vietstock,     Max plan)                │
  IR sites)                               ▼
                                    Neo4j  ◄──  cpkg.mcp_server  ──►  MCP client (workflow)
```

- **Crawl** grabs raw HTML (rate-limited, robots-aware) per ticker.
- **Extract** uses **Claude Code headless** (`claude -p --output-format json`, covered by
  your Max subscription) to turn the crawled text into structured `profile.json` +
  `governance.json`.
- **Tier-1** (`kgtier1.py`) writes a deterministic Cypher snapshot (`Company`, `Subsidiary`,
  `Figure`, `Shareholder`, `Officer`, `Dividend`, `InsiderDeal`, `AuditFirm`) keyed on
  stable IDs, with `OBSERVED_IN` history and the **name→ticker alias index**.
- **Tier-2** (`kggraphiti.py`) ingests narrative episodes into **Graphiti** using a **local
  Ollama** LLM + `bge-m3` embedder (free; the Max plan does not cover the Anthropic API
  Graphiti would otherwise call). Bi-temporal edges track how facts change over time.
- **MCP** (`src/cpkg/mcp_server.py`) serves both tiers; resolves names via Tier-1 first.

## Setup

```bash
cp .env.example .env            # fill NEO4J_PASSWORD; log in with `claude` once
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# Neo4j (Docker) + Ollama models:
docker run -d --name cpkg-neo4j -p 7687:7687 -p 7474:7474 \
  -e NEO4J_AUTH=neo4j/<password> neo4j:5
ollama pull bge-m3 && ollama pull qwen2.5:7b   # embedder + tier-2 extraction LLM
bash bin/init_db.sh             # constraints/indexes
```

## Run

```bash
python -m cpkg.pipeline --ticker VCB        # one ticker, end-to-end
bash bin/daily_update.sh                     # all tickers (cron entry)
```

Schedule daily: see `bin/daily_update.sh` header (macOS launchd plist + crontab line).

## Review UI

Inspect every layer the pipeline produces — per ticker & run — in the browser:

```bash
bash bin/review.sh                       # http://127.0.0.1:8765
bash bin/review.sh --port 9000 --host 0.0.0.0
```

Zero extra deps (stdlib `http.server`), read-only. Pick a ticker + run, then step
through the four stages side by side:

- **Crawl** — each crawled page (`data/raw/<ticker>/<date>/*.html`), toggling between
  the stripped text fed to Claude and the raw HTML.
- **Extract** — `extraction.json` rendered as structured sections (profile, governance,
  figures, `needs_confirm`, sources) plus a raw-JSON view.
- **Tier-1** — the Neo4j snapshot (Company + CrawlRun history, Figures, Subsidiaries,
  Shareholders, Officers, Dividends, InsiderDeals, AuditFirm).
- **Tier-2** — Graphiti episodes, extracted entities, and temporal facts (current vs
  superseded).

The filesystem layers (Crawl/Extract) work offline; the Neo4j layers (Tier-1/Tier-2)
degrade to an inline error if the DB is unreachable — the Neo4j pill in the header
shows live connectivity.

## MCP

Start it: `PYTHONPATH=src python -m cpkg.mcp_server` (stdio).

Add to a client's `.mcp.json` (the server lives in the `cpkg` package, not the `mcp`
SDK — invoke `cpkg.mcp_server`):

```json
{ "mcpServers": { "company-profile-kg": {
    "command": "/abs/path/to/company-profile-kg/.venv/bin/python",
    "args": ["-m", "cpkg.mcp_server"],
    "cwd": "/abs/path/to/company-profile-kg",
    "env": { "PYTHONPATH": "src" } } } }
```

Tools: `resolve_ticker`, `get_company_profile`, `get_subsidiaries`, `get_governance`,
`get_ownership`, `get_board`, `get_dividends`, `get_insider_deals`, `search_facts`,
`list_companies`.

## Config

- `config/tickers.yaml` — universe, names, alias seeds, IR URLs.
- `config/sources.yaml` — CafeF/Vietstock URL templates + page kinds.
- `.env` — Neo4j, Ollama, Claude, crawl settings.
