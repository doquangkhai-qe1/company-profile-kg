"""Tier-1 knowledge graph: deterministic Cypher layer (no LLM).

Writes a current snapshot keyed on stable IDs, with per-crawl `OBSERVED_IN` history:
  (:Company)-[:HAS_RUN]->(:CrawlRun)
  (:Company)-[:HAS_*]->(:Subsidiary|Shareholder|Officer|Dividend|InsiderDeal)
  (:Company)-[:AUDITED_BY]->(:AuditFirm)
  (:Figure|Subsidiary|Shareholder|...)-[:OBSERVED_IN {snapshot}]->(:CrawlRun)
Each node carries `last_seen` (latest crawl date that reported it) so "current" =
nodes whose last_seen == the company's latest crawl. Labels are disjoint from
Graphiti's (Entity/Episodic/Community) so both tiers share one Neo4j database.
"""
from __future__ import annotations

from .config import Config
from .schema import Extraction, normalize_name

CONNECT_TIMEOUT_S = 3.0

DDL = [
    "CREATE CONSTRAINT cpkg_company IF NOT EXISTS FOR (c:Company) REQUIRE c.ticker IS UNIQUE",
    "CREATE CONSTRAINT cpkg_run IF NOT EXISTS FOR (r:CrawlRun) REQUIRE r.uid IS UNIQUE",
    "CREATE CONSTRAINT cpkg_sub IF NOT EXISTS FOR (s:Subsidiary) REQUIRE s.uid IS UNIQUE",
    "CREATE CONSTRAINT cpkg_fig IF NOT EXISTS FOR (f:Figure) REQUIRE f.uid IS UNIQUE",
    "CREATE CONSTRAINT cpkg_sh IF NOT EXISTS FOR (s:Shareholder) REQUIRE s.uid IS UNIQUE",
    "CREATE CONSTRAINT cpkg_off IF NOT EXISTS FOR (o:Officer) REQUIRE o.uid IS UNIQUE",
    "CREATE CONSTRAINT cpkg_div IF NOT EXISTS FOR (d:Dividend) REQUIRE d.uid IS UNIQUE",
    "CREATE CONSTRAINT cpkg_ins IF NOT EXISTS FOR (i:InsiderDeal) REQUIRE i.uid IS UNIQUE",
    "CREATE CONSTRAINT cpkg_audit IF NOT EXISTS FOR (a:AuditFirm) REQUIRE a.name IS UNIQUE",
    "CREATE INDEX cpkg_company_norm IF NOT EXISTS FOR (c:Company) ON (c.name_norm)",
]


# ── driver ───────────────────────────────────────────────────────────────────

def connect(cfg: Config):
    from neo4j import GraphDatabase

    from .config import neo4j_security_kwargs

    driver = GraphDatabase.driver(
        cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password),
        connection_timeout=CONNECT_TIMEOUT_S, max_connection_lifetime=60,
        notifications_min_severity="OFF",
        **neo4j_security_kwargs(cfg),
    )
    driver.verify_connectivity()
    return driver


def ensure_schema(driver, database: str) -> int:
    for stmt in DDL:
        driver.execute_query(stmt, database_=database)
    return len(DDL)


def _run(driver, database, query, **params):
    records, _, _ = driver.execute_query(query, database_=database, **params)
    return records


# ── ingest ───────────────────────────────────────────────────────────────────

def _aliases_for(ext: Extraction, company: dict) -> list[str]:
    al = {ext.ticker, company.get("name", "")}
    al.update(company.get("aliases", []) or [])
    return sorted({a for a in al if a})


def ingest(driver, database: str, ext: Extraction, company: dict | None = None) -> dict:
    """Upsert one extraction into Tier-1. Idempotent per (ticker, crawl_date)."""
    company = company or {}
    ticker = ext.ticker.upper()
    crawl_date = ext.as_of or ""
    run_uid = f"{ticker}:{crawl_date}"
    aliases = _aliases_for(ext, company)
    name = company.get("name") or ticker
    name_norm = normalize_name(name)
    alias_norms = sorted({normalize_name(a) for a in aliases if a})

    counts: dict[str, int] = {}

    # Company + CrawlRun
    _run(driver, database, """
        MERGE (c:Company {ticker:$ticker})
          SET c.name=$name, c.name_norm=$name_norm, c.aliases=$aliases,
              c.alias_norms=$alias_norms, c.last_crawl=$crawl_date
        MERGE (r:CrawlRun {uid:$run_uid})
          SET r.ticker=$ticker, r.crawl_date=$crawl_date, r.as_of=$as_of
        MERGE (c)-[:HAS_RUN]->(r)
    """, ticker=ticker, name=name, name_norm=name_norm, aliases=aliases,
         alias_norms=alias_norms, crawl_date=crawl_date, run_uid=run_uid,
         as_of=ext.as_of or crawl_date)

    # Figures
    fig_rows = []
    for key, f in (ext.figures or {}).items():
        fig_rows.append({
            "uid": f"{ticker}:fig:{key}", "key": key, "ticker": ticker,
            "label_en": f.label_en, "label_vi": f.label_vi,
            "value_num": f.value_num, "value_str": f.value_str, "unit": f.unit,
            "scope": f.scope, "asof": f.asof, "source": f.source,
            "confidence": f.confidence, "last_seen": crawl_date,
        })
    if fig_rows:
        _run(driver, database, """
            UNWIND $rows AS row
            MATCH (c:Company {ticker:$ticker})
            MATCH (r:CrawlRun {uid:$run_uid})
            MERGE (f:Figure {uid:row.uid})
              SET f += {key:row.key, ticker:row.ticker, label_en:row.label_en,
                        label_vi:row.label_vi, value_num:row.value_num,
                        value_str:row.value_str, unit:row.unit, scope:row.scope,
                        asof:row.asof, source:row.source, confidence:row.confidence,
                        last_seen:row.last_seen}
            MERGE (c)-[:HAS_FIGURE]->(f)
            MERGE (f)-[obs:OBSERVED_IN]->(r)
              SET obs.value_num=row.value_num, obs.value_str=row.value_str,
                  obs.source=row.source, obs.confidence=row.confidence
        """, ticker=ticker, run_uid=run_uid, rows=fig_rows)
    counts["figures"] = len(fig_rows)

    # Subsidiaries (profile + governance lists merged on normalized name)
    sub_map: dict[str, dict] = {}
    for s in list(ext.profile.organisation.subsidiaries) + list(ext.governance.subsidiaries):
        nn = normalize_name(s.name)
        if not nn:
            continue
        sub_map[nn] = {
            "uid": f"{ticker}:sub:{nn}", "ticker": ticker, "name": s.name,
            "stake_pct": s.stake_pct, "line_vi": s.line_vi, "source": s.source,
            "last_seen": crawl_date,
        }
    if sub_map:
        _run(driver, database, """
            UNWIND $rows AS row
            MATCH (c:Company {ticker:$ticker})
            MATCH (r:CrawlRun {uid:$run_uid})
            MERGE (s:Subsidiary {uid:row.uid})
              SET s += {ticker:row.ticker, name:row.name, stake_pct:row.stake_pct,
                        line_vi:row.line_vi, source:row.source, last_seen:row.last_seen}
            MERGE (c)-[:HAS_SUBSIDIARY]->(s)
            MERGE (s)-[obs:OBSERVED_IN]->(r) SET obs.stake_pct=row.stake_pct
        """, ticker=ticker, run_uid=run_uid, rows=list(sub_map.values()))
    counts["subsidiaries"] = len(sub_map)

    # Shareholders
    sh_rows = [{
        "uid": f"{ticker}:sh:{normalize_name(s.holder_vi)}", "ticker": ticker,
        "holder_vi": s.holder_vi, "pct": s.pct, "is_foreign": s.is_foreign,
        "is_strategic": s.is_strategic, "source": s.source, "last_seen": crawl_date,
    } for s in ext.governance.ownership if s.holder_vi]
    if sh_rows:
        _run(driver, database, """
            UNWIND $rows AS row
            MATCH (c:Company {ticker:$ticker})
            MATCH (r:CrawlRun {uid:$run_uid})
            MERGE (s:Shareholder {uid:row.uid})
              SET s += {ticker:row.ticker, holder_vi:row.holder_vi, pct:row.pct,
                        is_foreign:row.is_foreign, is_strategic:row.is_strategic,
                        source:row.source, last_seen:row.last_seen}
            MERGE (c)-[:HAS_SHAREHOLDER]->(s)
            MERGE (s)-[obs:OBSERVED_IN]->(r) SET obs.pct=row.pct
        """, ticker=ticker, run_uid=run_uid, rows=sh_rows)
    counts["shareholders"] = len(sh_rows)

    # Officers
    off_rows = [{
        "uid": f"{ticker}:off:{normalize_name(o.name_vi)}:{normalize_name(o.role_vi or '')}",
        "ticker": ticker, "name_vi": o.name_vi, "role_vi": o.role_vi,
        "body": o.body, "independent": o.independent, "source": o.source,
        "last_seen": crawl_date,
    } for o in ext.governance.board if o.name_vi]
    if off_rows:
        _run(driver, database, """
            UNWIND $rows AS row
            MATCH (c:Company {ticker:$ticker})
            MATCH (r:CrawlRun {uid:$run_uid})
            MERGE (o:Officer {uid:row.uid})
              SET o += {ticker:row.ticker, name_vi:row.name_vi, role_vi:row.role_vi,
                        body:row.body, independent:row.independent, source:row.source,
                        last_seen:row.last_seen}
            MERGE (c)-[:HAS_OFFICER]->(o)
            MERGE (o)-[:OBSERVED_IN]->(r)
        """, ticker=ticker, run_uid=run_uid, rows=off_rows)
    counts["officers"] = len(off_rows)

    # Dividends
    div_rows = [{
        "uid": f"{ticker}:div:{d.year or '?'}:{d.type or '?'}", "ticker": ticker,
        "year": d.year, "type": d.type, "pct": d.pct, "note_vi": d.note_vi,
        "source": d.source, "last_seen": crawl_date,
    } for d in ext.governance.dividend_history if (d.year or d.pct is not None)]
    if div_rows:
        _run(driver, database, """
            UNWIND $rows AS row
            MATCH (c:Company {ticker:$ticker})
            MATCH (r:CrawlRun {uid:$run_uid})
            MERGE (d:Dividend {uid:row.uid})
              SET d += {ticker:row.ticker, year:row.year, type:row.type, pct:row.pct,
                        note_vi:row.note_vi, source:row.source, last_seen:row.last_seen}
            MERGE (c)-[:HAS_DIVIDEND]->(d)
            MERGE (d)-[:OBSERVED_IN]->(r)
        """, ticker=ticker, run_uid=run_uid, rows=div_rows)
    counts["dividends"] = len(div_rows)

    # Insider deals
    ins_rows = [{
        "uid": f"{ticker}:ins:{normalize_name(i.person_vi or '')}:{i.date or '?'}:{i.side or '?'}",
        "ticker": ticker, "person_vi": i.person_vi, "relation_vi": i.relation_vi,
        "side": i.side, "qty": i.qty, "date": i.date, "source": i.source,
        "last_seen": crawl_date,
    } for i in ext.governance.insider_dealing if (i.person_vi or i.date)]
    if ins_rows:
        _run(driver, database, """
            UNWIND $rows AS row
            MATCH (c:Company {ticker:$ticker})
            MATCH (r:CrawlRun {uid:$run_uid})
            MERGE (i:InsiderDeal {uid:row.uid})
              SET i += {ticker:row.ticker, person_vi:row.person_vi, relation_vi:row.relation_vi,
                        side:row.side, qty:row.qty, date:row.date, source:row.source,
                        last_seen:row.last_seen}
            MERGE (c)-[:HAS_INSIDER_DEAL]->(i)
            MERGE (i)-[:OBSERVED_IN]->(r)
        """, ticker=ticker, run_uid=run_uid, rows=ins_rows)
    counts["insider_deals"] = len(ins_rows)

    # Audit firm
    if ext.governance.audit_firm:
        _run(driver, database, """
            MATCH (c:Company {ticker:$ticker})
            MERGE (a:AuditFirm {name:$name})
            MERGE (c)-[ab:AUDITED_BY]->(a) SET ab.last_seen=$crawl_date
        """, ticker=ticker, name=ext.governance.audit_firm, crawl_date=crawl_date)
        counts["audit_firm"] = 1

    return {"ticker": ticker, "crawl_date": crawl_date, "run_uid": run_uid, "counts": counts}


# ── resolve + query (for the MCP server) ─────────────────────────────────────

def resolve_ticker(driver, database: str, query: str) -> dict:
    """Resolve a ticker OR company name to a canonical ticker (Tier-1, no LLM)."""
    q = (query or "").strip()
    qn = normalize_name(q)
    # 1) exact ticker
    rec = _run(driver, database,
               "MATCH (c:Company) WHERE toUpper(c.ticker)=toUpper($q) "
               "RETURN c.ticker AS ticker, c.name AS name", q=q)
    if rec:
        return {"ticker": rec[0]["ticker"], "name": rec[0]["name"],
                "confidence": "high", "candidates": []}
    # 2) exact normalized name / alias
    rec = _run(driver, database, """
        MATCH (c:Company)
        WHERE c.name_norm=$qn OR $qn IN c.alias_norms
        RETURN c.ticker AS ticker, c.name AS name LIMIT 5
    """, qn=qn)
    if rec:
        return {"ticker": rec[0]["ticker"], "name": rec[0]["name"],
                "confidence": "high",
                "candidates": [{"ticker": r["ticker"], "name": r["name"]} for r in rec[1:]]}
    # 3) fuzzy: query contains/contained-by a name or alias (either direction)
    rec = _run(driver, database, """
        MATCH (c:Company)
        WHERE c.name_norm CONTAINS $qn OR $qn CONTAINS c.name_norm
           OR any(a IN c.alias_norms WHERE a <> '' AND (a CONTAINS $qn OR $qn CONTAINS a))
        RETURN c.ticker AS ticker, c.name AS name LIMIT 5
    """, qn=qn)
    if rec:
        return {"ticker": rec[0]["ticker"], "name": rec[0]["name"],
                "confidence": "medium",
                "candidates": [{"ticker": r["ticker"], "name": r["name"]} for r in rec[1:]]}
    return {"ticker": None, "name": None, "confidence": "none", "candidates": []}


def _company_node(driver, database, ticker):
    rec = _run(driver, database,
               "MATCH (c:Company {ticker:$t}) RETURN c.ticker AS ticker, c.name AS name, "
               "c.aliases AS aliases, c.last_crawl AS last_crawl", t=ticker)
    return dict(rec[0]) if rec else None


def get_figures(driver, database, ticker) -> list[dict]:
    rec = _run(driver, database, """
        MATCH (:Company {ticker:$t})-[:HAS_FIGURE]->(f:Figure)
        RETURN f.key AS key, f.label_vi AS label_vi, f.value_num AS value_num,
               f.value_str AS value_str, f.unit AS unit, f.scope AS scope,
               f.source AS source, f.confidence AS confidence, f.last_seen AS last_seen
        ORDER BY f.scope, f.key
    """, t=ticker)
    return [dict(r) for r in rec]


def get_subsidiaries(driver, database, ticker) -> list[dict]:
    rec = _run(driver, database, """
        MATCH (:Company {ticker:$t})-[:HAS_SUBSIDIARY]->(s:Subsidiary)
        RETURN s.name AS name, s.stake_pct AS stake_pct, s.line_vi AS line_vi,
               s.source AS source, s.last_seen AS last_seen
        ORDER BY coalesce(s.stake_pct,0) DESC
    """, t=ticker)
    return [dict(r) for r in rec]


def get_ownership(driver, database, ticker) -> list[dict]:
    rec = _run(driver, database, """
        MATCH (:Company {ticker:$t})-[:HAS_SHAREHOLDER]->(s:Shareholder)
        RETURN s.holder_vi AS holder_vi, s.pct AS pct, s.is_foreign AS is_foreign,
               s.is_strategic AS is_strategic, s.source AS source, s.last_seen AS last_seen
        ORDER BY coalesce(s.pct,0) DESC
    """, t=ticker)
    return [dict(r) for r in rec]


def get_board(driver, database, ticker) -> list[dict]:
    rec = _run(driver, database, """
        MATCH (:Company {ticker:$t})-[:HAS_OFFICER]->(o:Officer)
        RETURN o.name_vi AS name_vi, o.role_vi AS role_vi, o.body AS body,
               o.independent AS independent, o.source AS source, o.last_seen AS last_seen
        ORDER BY o.body, o.role_vi
    """, t=ticker)
    return [dict(r) for r in rec]


def get_dividends(driver, database, ticker) -> list[dict]:
    rec = _run(driver, database, """
        MATCH (:Company {ticker:$t})-[:HAS_DIVIDEND]->(d:Dividend)
        RETURN d.year AS year, d.type AS type, d.pct AS pct, d.note_vi AS note_vi,
               d.source AS source
        ORDER BY d.year DESC
    """, t=ticker)
    return [dict(r) for r in rec]


def get_insider_deals(driver, database, ticker) -> list[dict]:
    rec = _run(driver, database, """
        MATCH (:Company {ticker:$t})-[:HAS_INSIDER_DEAL]->(i:InsiderDeal)
        RETURN i.person_vi AS person_vi, i.relation_vi AS relation_vi, i.side AS side,
               i.qty AS qty, i.date AS date, i.source AS source
        ORDER BY i.date DESC
    """, t=ticker)
    return [dict(r) for r in rec]


def get_audit_firm(driver, database, ticker) -> str | None:
    rec = _run(driver, database,
               "MATCH (:Company {ticker:$t})-[:AUDITED_BY]->(a:AuditFirm) "
               "RETURN a.name AS name ORDER BY a.name LIMIT 1", t=ticker)
    return rec[0]["name"] if rec else None


def list_companies(driver, database) -> list[dict]:
    rec = _run(driver, database,
               "MATCH (c:Company) RETURN c.ticker AS ticker, c.name AS name, "
               "c.last_crawl AS last_crawl ORDER BY c.ticker")
    return [dict(r) for r in rec]


def company_overview(driver, database, ticker) -> dict | None:
    c = _company_node(driver, database, ticker)
    if not c:
        return None
    c["figures"] = get_figures(driver, database, ticker)
    c["subsidiaries"] = get_subsidiaries(driver, database, ticker)
    return c
