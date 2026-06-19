"""MCP stdio server exposing the company-profile + governance knowledge graph.

Query by **ticker OR company name** (Tier-1 alias resolver runs first). Tier-1
(Neo4j Cypher) backs exact/list queries; Tier-2 (Graphiti) backs semantic
`search_facts` with temporal status. Every tool is degrade-safe: infra down →
{"ok": false, "error": ...}, never an exception across the wire.

Run:  PYTHONPATH=src python -m cpkg.mcp_server
"""
from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from . import kgtier1
from .config import load_config

mcp = FastMCP("company-profile-kg")
_cfg = load_config()

# Lazily-created, reused across calls (one event loop in the stdio server).
_driver = None
_graphiti = None


# ── infra helpers (degrade-safe) ─────────────────────────────────────────────

def _driver_sync():
    global _driver
    if _driver is None:
        _driver = kgtier1.connect(_cfg)
    else:
        try:
            _driver.verify_connectivity()
        except Exception:
            try:
                _driver.close()
            except Exception:
                pass
            _driver = kgtier1.connect(_cfg)
    return _driver


async def _t1(fn, *args):
    """Run a sync Tier-1 query off the event loop; wrap failures."""
    def call():
        return fn(_driver_sync(), _cfg.neo4j_database, *args)
    return await asyncio.to_thread(call)


async def _resolve(query: str) -> dict:
    return await _t1(kgtier1.resolve_ticker, query)


async def _ticker_of(query: str) -> tuple[str | None, dict]:
    r = await _resolve(query)
    return r.get("ticker"), r


async def _graphiti_inst():
    global _graphiti
    if _graphiti is None:
        from .kggraphiti import build_graphiti
        _graphiti = build_graphiti(_cfg)
    return _graphiti


# ── tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
async def resolve_ticker(query: str) -> dict:
    """Resolve a stock ticker OR company name/alias to a canonical ticker.

    query: e.g. "VCB", "Vietcombank", "Ngân hàng Ngoại thương".
    Returns {ticker, name, confidence, candidates}.
    """
    try:
        return {"ok": True, **await _resolve(query)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def list_companies() -> dict:
    """List companies currently in the knowledge graph (ticker, name, last crawl)."""
    try:
        return {"ok": True, "companies": await _t1(kgtier1.list_companies)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def get_company_profile(ticker_or_name: str, include_narrative: bool = True) -> dict:
    """Full narrative profile snapshot: identity, figures, subsidiaries (Tier-1) +
    latest narrative facts (Tier-2 Graphiti) for history/business-model/strategy/
    capital-plan/ESG. Accepts a ticker or a company name."""
    try:
        ticker, res = await _ticker_of(ticker_or_name)
        if not ticker:
            return {"ok": False, "error": "company not found", "resolve": res}
        overview = await _t1(kgtier1.company_overview, ticker)
        out = {"ok": True, "ticker": ticker, "resolve": res, **(overview or {})}
        if include_narrative:
            out["narrative"] = await _search(
                f"{ticker} lịch sử mô hình kinh doanh định hướng kế hoạch tăng vốn ESG", 12)
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def get_subsidiaries(ticker_or_name: str) -> dict:
    """Subsidiaries / associates with stake % and business line."""
    try:
        ticker, res = await _ticker_of(ticker_or_name)
        if not ticker:
            return {"ok": False, "error": "company not found", "resolve": res}
        return {"ok": True, "ticker": ticker,
                "subsidiaries": await _t1(kgtier1.get_subsidiaries, ticker)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def get_governance(ticker_or_name: str, include_narrative: bool = True) -> dict:
    """Corporate-actions & governance snapshot: ownership/FOL, board (HĐQT/BĐH/BKS),
    audit firm, dividends, insider deals (Tier-1) + related-party narrative (Tier-2)."""
    try:
        ticker, res = await _ticker_of(ticker_or_name)
        if not ticker:
            return {"ok": False, "error": "company not found", "resolve": res}
        out = {
            "ok": True, "ticker": ticker, "resolve": res,
            "ownership": await _t1(kgtier1.get_ownership, ticker),
            "board": await _t1(kgtier1.get_board, ticker),
            "audit_firm": await _t1(kgtier1.get_audit_firm, ticker),
            "dividend_history": await _t1(kgtier1.get_dividends, ticker),
            "insider_dealing": await _t1(kgtier1.get_insider_deals, ticker),
        }
        if include_narrative:
            out["related_party"] = await _search(
                f"{ticker} bên liên quan connected lending sở hữu đối tác chiến lược", 8)
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def get_ownership(ticker_or_name: str) -> dict:
    """Large shareholders with %, foreign/strategic flags."""
    return await _simple_list(ticker_or_name, kgtier1.get_ownership, "ownership")


@mcp.tool()
async def get_board(ticker_or_name: str) -> dict:
    """Board / management / supervisory board members and roles."""
    return await _simple_list(ticker_or_name, kgtier1.get_board, "board")


@mcp.tool()
async def get_dividends(ticker_or_name: str) -> dict:
    """Dividend history (cash vs stock, %, year)."""
    return await _simple_list(ticker_or_name, kgtier1.get_dividends, "dividend_history")


@mcp.tool()
async def get_insider_deals(ticker_or_name: str) -> dict:
    """Insider dealing records (person, relation, side, qty, date)."""
    return await _simple_list(ticker_or_name, kgtier1.get_insider_deals, "insider_dealing")


@mcp.tool()
async def search_facts(query: str, ticker_or_name: str = "", limit: int = 10) -> dict:
    """Semantic search over Graphiti temporal facts (both domains). Each fact carries
    a status: current|superseded. Optionally scope by ticker/company name."""
    try:
        scope = ""
        if ticker_or_name:
            ticker, _ = await _ticker_of(ticker_or_name)
            scope = (ticker or ticker_or_name) + " "
        return {"ok": True, "facts": await _search(scope + query, limit)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ── shared internals ─────────────────────────────────────────────────────────

async def _simple_list(ticker_or_name: str, fn, key: str) -> dict:
    try:
        ticker, res = await _ticker_of(ticker_or_name)
        if not ticker:
            return {"ok": False, "error": "company not found", "resolve": res}
        return {"ok": True, "ticker": ticker, key: await _t1(fn, ticker)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _search(query: str, limit: int) -> list[dict]:
    """Tier-2 Graphiti hybrid search; degrades to [] if Graphiti/Ollama unavailable."""
    from .kggraphiti import search_facts as _sf
    try:
        g = await _graphiti_inst()
        return await _sf(g, _cfg, query, limit)
    except Exception:  # noqa: BLE001
        return []


def main():
    if _cfg.missing:
        import sys
        print(f"missing env: {_cfg.missing} (env file: {_cfg.env_file})", file=sys.stderr)
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
