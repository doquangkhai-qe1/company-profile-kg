"""Configuration: .env discovery + validated settings + ticker/source loading.

Mirrors the resolution pattern of the reference KG (kgconfig.py): real environment
always wins, then a repo-root .env. Embedding dim is frozen (changing the embedder
means wipe + reingest).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # company-profile-kg/


def _load_env() -> str | None:
    from dotenv import load_dotenv

    candidates = []
    if os.environ.get("CPKG_ENV_FILE"):
        candidates.append(Path(os.environ["CPKG_ENV_FILE"]))
    candidates.append(REPO_ROOT / ".env")
    for p in candidates:
        if p.is_file():
            load_dotenv(p, override=False)
            return str(p)
    return None


@dataclass
class Config:
    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    # Path to a custom CA / server cert (PEM) to trust for TLS. "" = no TLS override.
    # Relative paths resolve against the repo root. Only applied for unencrypted
    # URI schemes (bolt:// / neo4j://); the +s/+ssc schemes configure TLS themselves.
    neo4j_tls_cert: str = ""
    # Graphiti tier-2 LLM provider (switchable): openai | anthropic | claude_code
    #   openai      → any OpenAI-compatible endpoint (local Ollama OR api.openai.com)
    #   anthropic   → Anthropic API (needs ANTHROPIC_API_KEY + the [anthropic] extra; paid)
    #   claude_code → the `claude` CLI, covered by the Max plan (no API key, runs local)
    llm_provider: str = "openai"
    llm_model: str = "qwen2.5:7b"      # main/medium-size model
    llm_small_model: str = ""          # light calls (dedup); falls back to llm_model
    llm_base_url: str = "http://localhost:11434/v1"  # openai provider only
    llm_api_key: str = "ollama"        # openai provider; anthropic reads ANTHROPIC_API_KEY
    # Embeddings always go through an OpenAI-compatible endpoint (Ollama) — neither the
    # Anthropic API nor Claude Code expose an embeddings endpoint.
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_llm_model: str = "qwen2.5:7b"  # legacy; kept as a fallback for llm_model
    embed_model: str = "bge-m3"
    embed_dim: int = 1024
    llm_max_tokens: int = 2048
    semaphore_limit: int = 4
    group_id: str = "company-profile"
    # Claude headless extraction
    claude_bin: str = "claude"
    claude_model: str = "claude-sonnet-4-6"
    claude_timeout_s: int = 600
    # Crawl
    crawl_rate_s: float = 3.0
    crawl_user_agent: str = "company-profile-kg/0.1 (+research)"
    crawl_timeout_s: int = 30
    # Paths
    tickers_file: str = ""
    sources_file: str = ""
    data_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "raw")
    env_file: str | None = None
    missing: list[str] = field(default_factory=list)

    def require(self, *names: str) -> list[str]:
        return [n for n in names if not getattr(self, n, None)]


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def load_config() -> Config:
    env_file = _load_env()
    os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
    cfg = Config(
        neo4j_uri=os.environ.get("NEO4J_URI", Config.neo4j_uri),
        neo4j_user=os.environ.get("NEO4J_USER", Config.neo4j_user),
        neo4j_password=os.environ.get("NEO4J_PASSWORD", ""),
        neo4j_database=os.environ.get("NEO4J_DATABASE", Config.neo4j_database),
        neo4j_tls_cert=os.environ.get("NEO4J_TLS_CERT", Config.neo4j_tls_cert),
        llm_provider=os.environ.get("LLM_PROVIDER", Config.llm_provider),
        llm_model=os.environ.get(
            "LLM_MODEL", os.environ.get("OLLAMA_LLM_MODEL", Config.llm_model)),
        llm_small_model=os.environ.get("LLM_SMALL_MODEL", Config.llm_small_model),
        llm_base_url=os.environ.get(
            "LLM_BASE_URL", os.environ.get("OLLAMA_BASE_URL", Config.llm_base_url)),
        llm_api_key=os.environ.get("LLM_API_KEY", Config.llm_api_key),
        ollama_base_url=os.environ.get("OLLAMA_BASE_URL", Config.ollama_base_url),
        ollama_llm_model=os.environ.get("OLLAMA_LLM_MODEL", Config.ollama_llm_model),
        embed_model=os.environ.get("EMBED_MODEL", Config.embed_model),
        embed_dim=_i("EMBEDDING_DIM", Config.embed_dim),
        llm_max_tokens=_i("LLM_MAX_TOKENS", Config.llm_max_tokens),
        semaphore_limit=_i("SEMAPHORE_LIMIT", Config.semaphore_limit),
        group_id=os.environ.get("KG_GROUP_ID", Config.group_id),
        claude_bin=os.environ.get("CLAUDE_BIN", Config.claude_bin),
        claude_model=os.environ.get("CLAUDE_MODEL", Config.claude_model),
        claude_timeout_s=_i("CLAUDE_TIMEOUT_S", Config.claude_timeout_s),
        crawl_rate_s=_f("CRAWL_RATE_S", Config.crawl_rate_s),
        crawl_user_agent=os.environ.get("CRAWL_USER_AGENT", Config.crawl_user_agent),
        crawl_timeout_s=_i("CRAWL_TIMEOUT_S", Config.crawl_timeout_s),
        tickers_file=os.environ.get("TICKERS_FILE", str(REPO_ROOT / "config" / "tickers.yaml")),
        sources_file=os.environ.get("SOURCES_FILE", str(REPO_ROOT / "config" / "sources.yaml")),
        env_file=env_file,
    )
    if not cfg.llm_small_model:
        cfg.llm_small_model = cfg.llm_model
    os.environ.setdefault("SEMAPHORE_LIMIT", str(cfg.semaphore_limit))
    cfg.missing = cfg.require("neo4j_password")
    return cfg


def neo4j_security_kwargs(cfg: Config) -> dict:
    """TLS kwargs for the neo4j driver, derived from cfg.neo4j_tls_cert.

    When a custom cert is configured and the URI uses an unencrypted scheme
    (bolt:// / neo4j://), enable encryption and pin trust to that cert. The
    +s/+ssc schemes already configure TLS, so passing these kwargs would raise —
    return nothing in that case. Returns {} when no cert is set.
    """
    cert = (cfg.neo4j_tls_cert or "").strip()
    if not cert:
        return {}
    scheme = cfg.neo4j_uri.split("://", 1)[0].lower()
    if scheme not in ("bolt", "neo4j"):
        return {}
    p = Path(cert)
    if not p.is_absolute():
        p = REPO_ROOT / p
    if not p.is_file():
        raise FileNotFoundError(f"NEO4J_TLS_CERT not found: {p}")
    from neo4j import TrustCustomCAs

    return {"encrypted": True, "trusted_certificates": TrustCustomCAs(str(p))}


# ── config file loaders ──────────────────────────────────────────────────────

def load_tickers(path: str | None = None) -> list[dict]:
    """Return the list of company dicts from tickers.yaml."""
    import yaml

    p = Path(path or load_config().tickers_file)
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("companies", [])


def load_sources(path: str | None = None) -> dict:
    """Return the sources mapping from sources.yaml."""
    import yaml

    p = Path(path or load_config().sources_file)
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("sources", {})


def find_company(ticker: str, tickers: list[dict] | None = None) -> dict | None:
    """Look up a company config row by ticker (case-insensitive)."""
    t = ticker.upper()
    for row in tickers or load_tickers():
        if str(row.get("ticker", "")).upper() == t:
            return row
    return None
