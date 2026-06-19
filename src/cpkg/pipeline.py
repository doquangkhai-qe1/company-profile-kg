"""End-to-end pipeline for one ticker: crawl → extract (Claude) → Tier-1 + Tier-2 ingest.

Resumable + degrade-safe:
  - crawl/extract artifacts cached under data/raw/<ticker>/<date>/
  - --from-cache reuses a saved extraction.json (skip crawl + Claude)
  - Tier-2 episodes are idempotent (skip by name); a Neo4j/Ollama outage doesn't lose Tier-1
Returns a result dict; CLI prints it as JSON and exits non-zero only on a hard failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from .config import find_company, load_config
from .crawl import crawl_ticker
from .extract import extract
from .schema import Extraction


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_cached_extraction(ticker: str, cfg, day: str | None) -> Extraction | None:
    base = cfg.data_dir / ticker.upper()
    if not base.exists():
        return None
    day_dir = base / day if day else None
    if not day_dir or not day_dir.exists():
        days = sorted([p for p in base.iterdir() if p.is_dir()], reverse=True)
        day_dir = days[0] if days else None
    if not day_dir:
        return None
    f = day_dir / "extraction.json"
    if not f.is_file():
        return None
    return Extraction.model_validate_json(f.read_text(encoding="utf-8"))


def _ingest_tier1(cfg, ext: Extraction) -> dict:
    from . import kgtier1
    driver = kgtier1.connect(cfg)
    try:
        kgtier1.ensure_schema(driver, cfg.neo4j_database)
        return kgtier1.ingest(driver, cfg.neo4j_database, ext,
                              company=find_company(ext.ticker))
    finally:
        driver.close()


def _ingest_tier2(cfg, ext: Extraction, max_episodes: int | None = None) -> dict:
    from .kggraphiti import build_episodes, build_graphiti, ingest_episodes

    episodes = build_episodes(ext)
    if max_episodes:
        episodes = episodes[: int(max_episodes)]
    if not episodes:
        return {"episodes_added": 0, "note": "no narrative sections"}

    async def _run():
        g = build_graphiti(cfg)
        try:
            return await ingest_episodes(g, cfg, episodes, ext.as_of or "")
        finally:
            await g.close()

    return asyncio.run(_run())


def run_ticker(ticker: str, *, do_tier2: bool = True, from_cache: bool = False,
               dry_run: bool = False, max_episodes: int | None = None, cfg=None) -> dict:
    cfg = cfg or load_config()
    ticker = ticker.upper()
    today = date.today().isoformat()
    out_dir = cfg.data_dir / ticker / today
    result: dict = {"ticker": ticker, "as_of": today, "ok": False, "warnings": []}

    # 1) obtain extraction (cache or crawl+Claude)
    if from_cache:
        ext = _load_cached_extraction(ticker, cfg, today)
        if not ext:
            result["warnings"].append("no cached extraction found")
            return result
        result["source"] = "cache"
    else:
        crawl = crawl_ticker(ticker, cfg)
        result["warnings"] += crawl.warnings
        result["crawl_pages"] = {"ok": len(crawl.ok_pages()), "total": len(crawl.pages)}
        if not crawl.ok_pages():
            result["warnings"].append("crawl produced no usable pages")
            return result
        ext, ext_warn = extract(crawl, cfg)
        result["warnings"] += ext_warn
        if not ext:
            result["warnings"].append("extraction failed")
            return result
        ext.as_of = today
        _save(out_dir / "extraction.json", json.loads(ext.model_dump_json()))
        result["source"] = "crawl"

    result["extraction_summary"] = {
        "figures": len(ext.figures), "subsidiaries": len(ext.profile.organisation.subsidiaries),
        "shareholders": len(ext.governance.ownership), "board": len(ext.governance.board),
        "dividends": len(ext.governance.dividend_history),
        "needs_confirm": len(ext.needs_confirm),
    }
    if dry_run:
        result["ok"] = True
        result["dry_run"] = True
        return result

    # 2) Tier-1 (deterministic; the source of truth for MCP exact queries)
    try:
        result["tier1"] = _ingest_tier1(cfg, ext)
        result["ok"] = True
    except Exception as e:  # noqa: BLE001
        result["warnings"].append(f"tier1 failed: {type(e).__name__}: {e}")
        return result  # tier1 is the gate; without it there's nothing to serve

    # 3) Tier-2 (Graphiti; optional — never fails the run)
    if do_tier2:
        try:
            result["tier2"] = _ingest_tier2(cfg, ext, max_episodes=max_episodes)
        except Exception as e:  # noqa: BLE001
            result["warnings"].append(f"tier2 degraded: {type(e).__name__}: {e}")
            result["tier2"] = {"ok": False}
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="company-profile-kg pipeline (one ticker)")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--no-tier2", action="store_true", help="skip Graphiti tier-2")
    ap.add_argument("--from-cache", action="store_true",
                    help="reuse saved extraction.json (skip crawl + Claude)")
    ap.add_argument("--dry-run", action="store_true",
                    help="crawl + extract only; write extraction.json, no DB ingest")
    ap.add_argument("--max-episodes", type=int, default=None,
                    help="cap tier-2 episodes (smoke tests / slow local LLM)")
    args = ap.parse_args(argv)

    cfg = load_config()
    if cfg.missing:
        print(json.dumps({"ok": False, "error": f"missing env: {cfg.missing}",
                          "env_file": cfg.env_file}), file=sys.stderr)
        return 2

    res = run_ticker(args.ticker, do_tier2=not args.no_tier2,
                     from_cache=args.from_cache, dry_run=args.dry_run,
                     max_episodes=args.max_episodes, cfg=cfg)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
