"""Crawler: fetch public profile/governance pages per ticker.

Polite by design: respects robots.txt, rate-limits, sets a custom UA, retries with
backoff. Returns extracted *text* (HTML stripped) per page — the heavy parsing is
delegated to Claude in extract.py. Unreachable/thin pages are warnings, not errors.
"""
from __future__ import annotations

import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from .config import Config, find_company, load_config, load_sources


@dataclass
class Page:
    kind: str          # profile | shareholders | officers | dividends | insider | subsidiaries | website
    source: str        # cafef | vietstock | website
    url: str
    status: int = 0
    text: str = ""
    error: str | None = None
    html_path: str | None = None


@dataclass
class CrawlResult:
    ticker: str
    fetched_at: str
    pages: list[Page] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok_pages(self) -> list[Page]:
        return [p for p in self.pages if p.text and not p.error]


_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def _robots_ok(url: str, ua: str) -> bool:
    """Best-effort robots.txt check; on any failure, allow (sites often lack one)."""
    try:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = _ROBOTS_CACHE.get(base, "missing")
        if rp == "missing":
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(base + "/robots.txt")
            try:
                rp.read()
            except Exception:
                rp = None
            _ROBOTS_CACHE[base] = rp
        if rp is None:
            return True
        return rp.can_fetch(ua, url)
    except Exception:
        return True


def _html_to_text(html: str) -> str:
    """Strip scripts/styles/nav, collapse whitespace — keep tables/labels readable."""
    tree = HTMLParser(html)
    for tag in tree.css("script, style, noscript, svg, iframe"):
        tag.decompose()
    text = tree.body.text(separator="\n") if tree.body else tree.text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    out, blank = [], 0
    for ln in lines:
        if not ln:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def _fetch(client: httpx.Client, url: str, cfg: Config) -> tuple[int, str, str | None]:
    last_err = None
    for attempt in range(3):
        try:
            r = client.get(url)
            return r.status_code, r.text, None
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(cfg.crawl_rate_s * (attempt + 1))
    return 0, "", last_err


def _page_targets(ticker: str, company: dict, sources: dict) -> list[tuple[str, str, str]]:
    """Yield (source, kind, url) for every enabled page of this ticker.

    Vietstock uses clean {ticker} templates. CafeF profile URLs require a per-company
    slug, so they are taken from `cafef_url` in tickers.yaml (skipped if absent) rather
    than guessed — avoids 404 spam. The company `website` is crawled too.
    """
    out: list[tuple[str, str, str]] = []
    vs = sources.get("vietstock") or {}
    if vs.get("enabled", True):
        for kind, tmpl in (vs.get("pages") or {}).items():
            out.append(("vietstock", kind, tmpl.format(ticker=ticker)))
    if (sources.get("cafef") or {}).get("enabled", True) and company.get("cafef_url"):
        out.append(("cafef", "profile", company["cafef_url"]))
    if (sources.get("website") or {}).get("enabled", True) and company.get("website"):
        out.append(("website", "website", company["website"]))
    return out


def crawl_ticker(ticker: str, cfg: Config | None = None,
                 save_dir: Path | None = None) -> CrawlResult:
    """Crawl all configured pages for one ticker. Never raises."""
    cfg = cfg or load_config()
    ticker = ticker.upper()
    company = find_company(ticker) or {"ticker": ticker}
    sources = load_sources()
    today = date.today().isoformat()
    result = CrawlResult(ticker=ticker, fetched_at=today)

    if save_dir is None:
        save_dir = cfg.data_dir / ticker / today
    save_dir.mkdir(parents=True, exist_ok=True)

    headers = {"User-Agent": cfg.crawl_user_agent,
               "Accept-Language": "vi,en;q=0.8"}
    with httpx.Client(headers=headers, timeout=cfg.crawl_timeout_s,
                      follow_redirects=True) as client:
        for src_name, kind, url in _page_targets(ticker, company, sources):
            page = Page(kind=kind, source=src_name, url=url)
            if not _robots_ok(url, cfg.crawl_user_agent):
                page.error = "blocked by robots.txt"
                result.pages.append(page)
                result.warnings.append(f"{src_name}/{kind}: robots.txt disallow")
                continue
            status, html, err = _fetch(client, url, cfg)
            page.status = status
            if err:
                page.error = err
                result.warnings.append(f"{src_name}/{kind}: {err}")
            elif status >= 400:
                page.error = f"HTTP {status}"
                result.warnings.append(f"{src_name}/{kind}: HTTP {status}")
            else:
                page.text = _html_to_text(html)
                raw = save_dir / f"{src_name}_{kind}.html"
                try:
                    raw.write_text(html, encoding="utf-8")
                    page.html_path = str(raw)
                except Exception as e:  # noqa: BLE001
                    result.warnings.append(f"save {raw.name}: {e}")
                if len(page.text) < 200:
                    result.warnings.append(
                        f"{src_name}/{kind}: thin content ({len(page.text)} chars) "
                        f"— page may be JS-rendered")
            result.pages.append(page)
            time.sleep(cfg.crawl_rate_s)
    return result


def combined_text(result: CrawlResult, max_chars: int = 60000) -> str:
    """Concatenate page texts with provenance headers, capped for the LLM prompt."""
    chunks, total = [], 0
    for p in result.ok_pages():
        header = f"\n===== SOURCE: {p.source} | PAGE: {p.kind} | URL: {p.url} =====\n"
        body = p.text
        budget = max_chars - total - len(header)
        if budget <= 0:
            break
        if len(body) > budget:
            body = body[:budget] + "\n…[truncated]"
        chunks.append(header + body)
        total += len(header) + len(body)
    return "".join(chunks)
