"""Pydantic schemas for the two fact domains + shared helpers.

Mirrors the structured output of the two reference agents:
  - bank-company-profile-extractor  -> ProfileDomain
  - bank-corpactions-gov-extractor   -> GovernanceDomain

Fields are permissive (Optional) because crawled aggregator data is incomplete;
the discipline (FACTS + source URL, claim_type fact|guidance, confidence) is kept.
Keys are EN, prose values VI ("*_vi").
"""
from __future__ import annotations

import re
import unicodedata
from typing import Literal, Optional

from pydantic import BaseModel, Field

Claim = Literal["fact", "guidance", "opinion", "estimate"]
Confidence = Literal["high", "medium", "low"]


# ── shared ───────────────────────────────────────────────────────────────────

class Source(BaseModel):
    id: Optional[str] = None
    kind: Optional[str] = None  # cafef | vietstock | website
    url: Optional[str] = None
    ref: Optional[str] = None
    retrieved: Optional[str] = None  # ISO date


class Figure(BaseModel):
    label_en: Optional[str] = None
    label_vi: Optional[str] = None
    value_num: Optional[float] = None
    value_str: Optional[str] = None
    unit: Optional[str] = None
    scope: Optional[str] = None  # profile | ownership | esg | financials | detail
    entity: Optional[str] = None
    asof: Optional[str] = None
    source: Optional[str] = None
    confidence: Confidence = "medium"


# ── profile domain ───────────────────────────────────────────────────────────

class Subsidiary(BaseModel):
    name: str
    stake_pct: Optional[float] = None
    line_vi: Optional[str] = None  # business line
    source: Optional[str] = None


class Organisation(BaseModel):
    structure_vi: Optional[str] = None
    employees: Optional[int] = None
    branches: Optional[int] = None
    pgd: Optional[int] = None
    atm: Optional[int] = None
    provinces: Optional[int] = None
    subsidiaries: list[Subsidiary] = Field(default_factory=list)
    source: Optional[str] = None


class StrategyDirection(BaseModel):
    vision_vi: Optional[str] = None
    targets: dict = Field(default_factory=dict)  # free-form {metric: target_str}
    claim_type: Claim = "guidance"
    source: Optional[str] = None


class CapitalComponent(BaseModel):
    type: Optional[str] = None  # private_placement | stock_dividend | retained_earnings_capitalization
    size_vi: Optional[str] = None
    partner_vi: Optional[str] = None
    status_vi: Optional[str] = None
    source: Optional[str] = None


class CapitalPlan(BaseModel):
    components: list[CapitalComponent] = Field(default_factory=list)
    charter_capital_str: Optional[str] = None
    charter_capital_num: Optional[float] = None
    source: Optional[str] = None


class EsgFacts(BaseModel):
    green_credit_vi: Optional[str] = None
    framework_vi: Optional[str] = None
    social_vi: Optional[str] = None
    source: Optional[str] = None


class ProfileDomain(BaseModel):
    history_vi: Optional[str] = None
    business_model_vi: Optional[str] = None
    organisation: Organisation = Field(default_factory=Organisation)
    strategy_direction: StrategyDirection = Field(default_factory=StrategyDirection)
    capital_plan: CapitalPlan = Field(default_factory=CapitalPlan)
    esg_facts: EsgFacts = Field(default_factory=EsgFacts)


# ── governance / corporate-actions domain ────────────────────────────────────

class Shareholder(BaseModel):
    holder_vi: str
    pct: Optional[float] = None
    is_foreign: Optional[bool] = None
    is_strategic: Optional[bool] = None
    source: Optional[str] = None


class Officer(BaseModel):
    name_vi: str
    role_vi: Optional[str] = None  # Chủ tịch / TGĐ / CFO / Thành viên...
    body: Optional[Literal["HĐQT", "BĐH", "BKS"]] = None
    independent: Optional[bool] = None
    source: Optional[str] = None


class Dividend(BaseModel):
    year: Optional[str] = None
    type: Optional[Literal["cash", "stock", "mixed"]] = None
    pct: Optional[float] = None
    note_vi: Optional[str] = None
    source: Optional[str] = None


class InsiderDeal(BaseModel):
    person_vi: Optional[str] = None
    relation_vi: Optional[str] = None
    side: Optional[Literal["buy", "sell", "registered"]] = None
    qty: Optional[float] = None
    date: Optional[str] = None
    source: Optional[str] = None


class GovernanceDomain(BaseModel):
    ownership: list[Shareholder] = Field(default_factory=list)
    foreign_room_vi: Optional[str] = None
    strategic_partner_vi: Optional[str] = None
    board: list[Officer] = Field(default_factory=list)
    audit_firm: Optional[str] = None
    dividend_history: list[Dividend] = Field(default_factory=list)
    insider_dealing: list[InsiderDeal] = Field(default_factory=list)
    subsidiaries: list[Subsidiary] = Field(default_factory=list)
    related_party_vi: Optional[str] = None


# ── extraction envelope (what Claude returns + what we ingest) ────────────────

class Extraction(BaseModel):
    ticker: str
    as_of: Optional[str] = None  # ISO date of the crawl/extract
    profile: ProfileDomain = Field(default_factory=ProfileDomain)
    governance: GovernanceDomain = Field(default_factory=GovernanceDomain)
    figures: dict[str, Figure] = Field(default_factory=dict)
    needs_confirm: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)


# ── helpers ──────────────────────────────────────────────────────────────────

def normalize_name(s: str) -> str:
    """Lowercase + strip diacritics + collapse whitespace — the name->ticker key."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")  # drop combining marks
    s = s.replace("đ", "d").replace("Đ", "d")
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return re.sub(r"\s+", " ", s)


_VN_NUM = re.compile(r"-?\d{1,3}(\.\d{3})*(,\d+)?|-?\d+(,\d+)?")


def parse_vn_number(value) -> float | None:
    """'46.823,3' -> 46823.3 ; '1,76%' -> 1.76 ; passthrough numerics."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    m = _VN_NUM.search(value.replace(" ", ""))
    if not m:
        return None
    return float(m.group(0).replace(".", "").replace(",", "."))


def extraction_json_skeleton() -> str:
    """A compact JSON skeleton handed to Claude so it returns the exact shape.

    Kept terse on purpose (token budget); the field semantics live in the prompt.
    """
    return (
        '{\n'
        '  "ticker": "<MÃ>",\n'
        '  "as_of": "<YYYY-MM-DD>",\n'
        '  "profile": {\n'
        '    "history_vi": "...", "business_model_vi": "...",\n'
        '    "organisation": {"structure_vi":"...","employees":0,"branches":0,"pgd":0,'
        '"atm":0,"provinces":0,"subsidiaries":[{"name":"","stake_pct":0.0,"line_vi":"","source":"<url>"}],"source":"<url>"},\n'
        '    "strategy_direction": {"vision_vi":"...","targets":{"ROE":"","NIM":""},'
        '"claim_type":"guidance","source":"<url>"},\n'
        '    "capital_plan": {"components":[{"type":"","size_vi":"","partner_vi":"","status_vi":"","source":"<url>"}],'
        '"charter_capital_str":"","charter_capital_num":0,"source":"<url>"},\n'
        '    "esg_facts": {"green_credit_vi":"","framework_vi":"","social_vi":"","source":"<url>"}\n'
        '  },\n'
        '  "governance": {\n'
        '    "ownership": [{"holder_vi":"","pct":0.0,"is_foreign":false,"is_strategic":false,"source":"<url>"}],\n'
        '    "foreign_room_vi":"", "strategic_partner_vi":"",\n'
        '    "board": [{"name_vi":"","role_vi":"","body":"HĐQT","independent":false,"source":"<url>"}],\n'
        '    "audit_firm":"",\n'
        '    "dividend_history": [{"year":"","type":"stock","pct":0.0,"note_vi":"","source":"<url>"}],\n'
        '    "insider_dealing": [{"person_vi":"","relation_vi":"","side":"buy","qty":0,"date":"","source":"<url>"}],\n'
        '    "subsidiaries": [{"name":"","stake_pct":0.0,"line_vi":"","source":"<url>"}],\n'
        '    "related_party_vi":""\n'
        '  },\n'
        '  "figures": {"EMPLOYEES":{"label_en":"Headcount","label_vi":"Số nhân viên","value_num":0,'
        '"value_str":"","unit":"persons","scope":"profile","asof":"","source":"<url>","confidence":"medium"}},\n'
        '  "needs_confirm": ["..."],\n'
        '  "sources": [{"id":"src.cafef.profile","kind":"cafef","url":"<url>","retrieved":"<YYYY-MM-DD>"}]\n'
        '}'
    )
