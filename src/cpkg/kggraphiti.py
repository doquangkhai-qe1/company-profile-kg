"""Tier-2 knowledge graph: Graphiti factory, ontology, episode builders, recall.

LLM + embedder both go through the local Ollama OpenAI-compatible endpoint (free;
the Claude Max subscription does not cover the Anthropic API Graphiti would call).
Cross-encoder is a no-op (recall uses RRF recipes). Carries over the hard-won fixes
from the reference KG: ClampedGenericClient (graphiti hardcodes edge max_tokens=16384),
field-less entity types (skip per-entity attribute LLM calls), small episode bodies.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from .config import Config
from .schema import Extraction

# ── ontology (classification-only: no fields → skips attribute-extraction LLM) ──


class Company(BaseModel):
    """A listed Vietnamese bank (subject or related), e.g. VCB, BID, CTG."""


class Subsidiary(BaseModel):
    """A subsidiary/associate: securities, leasing, insurance, AMC, remittance…"""


class Person(BaseModel):
    """Executives/board: Chủ tịch HĐQT, TGĐ/CEO, CFO, thành viên BKS."""


class Shareholder(BaseModel):
    """Institutional/strategic shareholders: NHNN, Mizuho, MUFG, SCIC, funds."""


class BusinessSegment(BaseModel):
    """Customer/segment lines: bán buôn, bán lẻ, SME, treasury, digital."""


class StrategyTarget(BaseModel):
    """Disclosed targets/vision: ROE/NIM/CASA/CAR goals, top-rank ambitions."""


class CapitalAction(BaseModel):
    """Capital actions: phát hành riêng lẻ, cổ tức cổ phiếu, tăng vốn điều lệ."""


class AuditFirm(BaseModel):
    """External auditor: EY, KPMG, Deloitte, PwC."""


class RelatedParty(BaseModel):
    """Related-party / connected-lending counterparties and ecosystem firms."""


class GovernmentBody(BaseModel):
    """Regulators/state bodies: NHNN (SBV), Bộ Tài chính, Chính phủ."""


ENTITY_TYPES = {
    "Company": Company, "Subsidiary": Subsidiary, "Person": Person,
    "Shareholder": Shareholder, "BusinessSegment": BusinessSegment,
    "StrategyTarget": StrategyTarget, "CapitalAction": CapitalAction,
    "AuditFirm": AuditFirm, "RelatedParty": RelatedParty,
    "GovernmentBody": GovernmentBody,
}

EXTRACTION_INSTRUCTIONS = (
    "Domain: Vietnamese listed-bank company profile + governance. Glossary: NHNN = "
    "Ngân hàng Nhà nước = State Bank of Vietnam (GovernmentBody); 3-letter uppercase "
    "codes (VCB, BID, CTG, TCB, MBB...) are listed banks (Company); Vietcombank=VCB, "
    "BIDV=BID, Vietinbank=CTG, Techcombank=TCB. HĐQT=board, BĐH=management, BKS=supervisory "
    "board. Keep facts in Vietnamese as written. Numbers: tỷ đồng = billion VND."
)


# ── factory ──────────────────────────────────────────────────────────────────

def build_graphiti(cfg: Config):
    """Construct a Graphiti instance wired for Ollama LLM + embedder. No network calls."""
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    class NoopCrossEncoder(CrossEncoderClient):
        async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
            return [(p, 1.0) for p in passages]

    class ClampedGenericClient(OpenAIGenericClient):
        """Clamp max_tokens from every call site.

        graphiti hardcodes extract_edges_max_tokens=16384 and passes it explicitly,
        bypassing the constructor default — small local models choke on that. Clamp
        to the configured budget everywhere.
        """

        async def generate_response(self, messages, response_model=None,
                                    max_tokens=None, **kwargs):
            clamp = self.max_tokens
            mt = clamp if max_tokens is None else min(max_tokens, clamp)
            return await super().generate_response(
                messages, response_model=response_model, max_tokens=mt, **kwargs)

    llm = ClampedGenericClient(
        config=LLMConfig(api_key="ollama", model=cfg.ollama_llm_model,
                         base_url=cfg.ollama_base_url, temperature=0,
                         max_tokens=cfg.llm_max_tokens),
        max_tokens=cfg.llm_max_tokens,
        structured_output_mode="json_object")
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(
        embedding_model=cfg.embed_model, api_key="ollama",
        base_url=cfg.ollama_base_url, embedding_dim=cfg.embed_dim))
    return Graphiti(cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_password,
                    llm_client=llm, embedder=embedder,
                    cross_encoder=NoopCrossEncoder(),
                    max_coroutines=cfg.semaphore_limit)


# ── episode builders ─────────────────────────────────────────────────────────

def _clip(s, n=400):
    s = str(s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


MAX_BODY = 2500
MAX_LINES = 6


def _chunks(lines: list[str]):
    cur, cur_len = [], 0
    for ln in [x for x in lines if x]:
        if cur and (cur_len + len(ln) > MAX_BODY or len(cur) >= MAX_LINES):
            yield cur
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += len(ln)
    if cur:
        yield cur


def build_episodes(ext: Extraction) -> list[dict]:
    """One episode per section across BOTH domains. Bodies kept small + chunked."""
    ticker, as_of = ext.ticker.upper(), (ext.as_of or "")
    episodes: list[dict] = []

    def add(section: str, header: str, lines: list[str]):
        for i, chunk in enumerate(_chunks(lines)):
            suffix = "" if i == 0 else f"_{i + 1}"
            episodes.append({
                "name": f"{ticker}.{section}{suffix}@{as_of}",
                "body": header + "\n" + "\n".join(f"- {ln}" for ln in chunk),
                "source_description": f"company-profile-kg crawl | {ticker} | {as_of}",
            })

    p = ext.profile
    if p.history_vi:
        add("history", f"Lịch sử hình thành {ticker} (tới {as_of}):", [_clip(p.history_vi, 1200)])
    if p.business_model_vi:
        add("business_model", f"Mô hình kinh doanh {ticker}:", [_clip(p.business_model_vi, 1200)])

    org = p.organisation
    org_lines = []
    if org.structure_vi:
        org_lines.append(_clip(org.structure_vi, 800))
    for label, val in (("Số nhân viên", org.employees), ("Chi nhánh", org.branches),
                       ("PGD", org.pgd), ("ATM", org.atm), ("Tỉnh/TP", org.provinces)):
        if val is not None:
            org_lines.append(f"{label}: {val}")
    for s in org.subsidiaries:
        org_lines.append(f"Công ty con: {s.name}"
                         + (f" ({s.stake_pct}%)" if s.stake_pct is not None else "")
                         + (f" — {s.line_vi}" if s.line_vi else ""))
    if org_lines:
        add("organisation", f"Mô hình tổ chức & nhân sự {ticker}:", org_lines)

    sd = p.strategy_direction
    sd_lines = []
    if sd.vision_vi:
        sd_lines.append(f"[guidance] Tầm nhìn: {_clip(sd.vision_vi, 600)}")
    for k, v in (sd.targets or {}).items():
        sd_lines.append(f"[guidance] Mục tiêu {k}: {v}")
    if sd_lines:
        add("strategy_direction", f"Định hướng phát triển {ticker} (claim công ty):", sd_lines)

    cp = p.capital_plan
    cp_lines = []
    if cp.charter_capital_str:
        cp_lines.append(f"Vốn điều lệ: {cp.charter_capital_str}")
    for c in cp.components:
        cp_lines.append(f"{c.type or 'cấu phần'}: {_clip(c.size_vi, 200)}"
                        + (f" — đối tác {c.partner_vi}" if c.partner_vi else "")
                        + (f" — {c.status_vi}" if c.status_vi else ""))
    if cp_lines:
        add("capital_plan", f"Kế hoạch tăng vốn {ticker}:", cp_lines)

    esg = p.esg_facts
    esg_lines = [x for x in (
        (f"Tín dụng xanh: {esg.green_credit_vi}" if esg.green_credit_vi else None),
        (f"Khung PTBV: {esg.framework_vi}" if esg.framework_vi else None),
        (f"Xã hội: {esg.social_vi}" if esg.social_vi else None)) if x]
    if esg_lines:
        add("esg_facts", f"ESG {ticker}:", [_clip(x, 500) for x in esg_lines])

    g = ext.governance
    own_lines = []
    if g.foreign_room_vi:
        own_lines.append(f"Room ngoại: {g.foreign_room_vi}")
    if g.strategic_partner_vi:
        own_lines.append(f"Đối tác chiến lược: {g.strategic_partner_vi}")
    for s in g.ownership:
        tag = "nước ngoài" if s.is_foreign else ""
        tag = (tag + " chiến lược").strip() if s.is_strategic else tag
        own_lines.append(f"Cổ đông {s.holder_vi}: {s.pct}%" + (f" ({tag})" if tag else ""))
    if own_lines:
        add("ownership", f"Cơ cấu sở hữu {ticker} (tới {as_of}):", own_lines)

    board_lines = [
        f"{o.role_vi or 'Thành viên'} ({o.body or '?'}): {o.name_vi}"
        + (" [độc lập]" if o.independent else "")
        for o in g.board if o.name_vi]
    if g.audit_firm:
        board_lines.append(f"Kiểm toán: {g.audit_firm}")
    if board_lines:
        add("board", f"HĐQT/BĐH/BKS & kiểm toán {ticker}:", board_lines)

    ins_lines = [
        f"{i.person_vi or '?'} ({i.relation_vi or '?'}) {i.side or '?'} "
        f"{i.qty or '?'} @ {i.date or '?'}"
        for i in g.insider_dealing if (i.person_vi or i.date)]
    if ins_lines:
        add("insider", f"Giao dịch nội bộ {ticker}:", ins_lines)

    div_lines = [f"{d.year or '?'}: {d.type or '?'} {d.pct}%"
                 + (f" — {d.note_vi}" if d.note_vi else "")
                 for d in g.dividend_history if (d.year or d.pct is not None)]
    if div_lines:
        add("dividend_policy", f"Lịch sử cổ tức {ticker}:", div_lines)

    if g.related_party_vi:
        add("related_party", f"Bên liên quan / connected-lending {ticker}:",
            [_clip(g.related_party_vi, 1200)])

    return episodes


def parse_reference_time(as_of: str) -> datetime:
    try:
        return datetime.fromisoformat(as_of).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


async def existing_episode_names(graphiti, cfg: Config, names: list[str]) -> set[str]:
    records, _, _ = await graphiti.driver.execute_query(
        "MATCH (e:Episodic {group_id:$gid}) WHERE e.name IN $names RETURN e.name AS name",
        gid=cfg.group_id, names=names)
    return {r["name"] for r in records}


async def ingest_episodes(graphiti, cfg: Config, episodes: list[dict], as_of: str) -> dict:
    """Add episodes sequentially (graphiti requirement), resumable + degrade-safe."""
    from graphiti_core.llm_client.errors import RateLimitError
    from graphiti_core.nodes import EpisodeType

    ref_time = parse_reference_time(as_of)
    done = await existing_episode_names(graphiti, cfg, [e["name"] for e in episodes])
    counts = {"episodes_added": 0, "episodes_skipped": 0, "entities": 0, "edges": 0,
              "failed": [], "remaining": 0, "stopped_early": False}
    for i, ep in enumerate(episodes):
        if ep["name"] in done:
            counts["episodes_skipped"] += 1
            continue
        try:
            result = await graphiti.add_episode(
                name=ep["name"], episode_body=ep["body"],
                source_description=ep["source_description"],
                reference_time=ref_time, source=EpisodeType.text,
                group_id=cfg.group_id, entity_types=ENTITY_TYPES,
                custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
                previous_episode_uuids=[])
        except RateLimitError:
            counts["remaining"] = len(episodes) - i
            counts["stopped_early"] = True
            break
        except Exception as e:  # noqa: BLE001
            counts["failed"].append({"name": ep["name"], "error": f"{type(e).__name__}: {e}"})
            continue
        counts["episodes_added"] += 1
        counts["entities"] += len(result.nodes or [])
        counts["edges"] += len(result.edges or [])
    return counts


# ── recall helpers ───────────────────────────────────────────────────────────

def _edge_dict(edge) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {"fact": edge.fact,
            "valid_at": iso(getattr(edge, "valid_at", None)),
            "invalid_at": iso(getattr(edge, "invalid_at", None)),
            "status": "superseded" if getattr(edge, "invalid_at", None) else "current"}


async def search_facts(graphiti, cfg: Config, query: str, limit: int = 10) -> list[dict]:
    edges = await graphiti.search(query=query, group_ids=[cfg.group_id], num_results=limit)
    return [_edge_dict(e) for e in edges]


async def search_nodes(graphiti, cfg: Config, query: str, limit: int = 8) -> list[dict]:
    from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF

    config = NODE_HYBRID_SEARCH_RRF.model_copy(deep=True)
    config.limit = limit
    results = await graphiti.search_(query=query, config=config, group_ids=[cfg.group_id])
    return [{"name": n.name, "summary": getattr(n, "summary", None)}
            for n in (results.nodes or [])]
