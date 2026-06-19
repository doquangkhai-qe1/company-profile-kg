"""Extraction via Claude Code headless (`claude -p`).

Turns crawled page text into a validated `Extraction` (profile + governance domains).
Uses the Claude CLI so it runs on the Max subscription (no Anthropic API billing).
Never raises: returns (extraction|None, warnings).
"""
from __future__ import annotations

import json
import subprocess
from datetime import date

from .config import Config, find_company, load_config
from .crawl import CrawlResult, combined_text
from .schema import Extraction, extraction_json_skeleton

_SYSTEM_RULES = (
    "Bạn là bộ trích xuất HỒ SƠ DOANH NGHIỆP + CORPORATE-ACTIONS/GOVERNANCE cho ngân hàng "
    "niêm yết Việt Nam. Đọc văn bản đã crawl từ CafeF/Vietstock/website công ty và bóc DỮ KIỆN.\n"
    "LUẬT CỨNG:\n"
    "- CHỈ FACTS có trong nguồn + locator (URL). TUYỆT ĐỐI KHÔNG suy diễn/đánh giá/bịa số.\n"
    "- Keys tiếng Anh, prose tiếng Việt (trường *_vi).\n"
    "- Mỗi fact gắn `source` = URL trang chứa nó. Mục tiêu/định hướng công ty = claim_type:guidance.\n"
    "- Không tìm thấy → bỏ trống / đưa vào needs_confirm. KHÔNG điền số phỏng đoán.\n"
    "- Số kiểu VN '46.823,3' giữ ở value_str; value_num là số thực (46823.3).\n"
    "- Nguồn là aggregator (thứ cấp) → confidence mặc định 'medium', số khớp nhiều nguồn → 'high'.\n"
    "PHÂN VAI: profile = tường thuật (lịch sử/mô hình KD/tổ chức/định hướng/vốn/ESG); "
    "governance = cổ đông/FOL/HĐQT-BĐH-BKS/audit/cổ tức/giao dịch nội bộ/cty con/bên liên quan."
)


def build_prompt(ticker: str, company: dict, crawl_text: str) -> str:
    name = company.get("name", "")
    return (
        f"{_SYSTEM_RULES}\n\n"
        f"CÔNG TY: {ticker} — {name}\n"
        f"NGÀY: {date.today().isoformat()}\n\n"
        "TRẢ VỀ DUY NHẤT một JSON object đúng shape sau (không markdown, không giải thích, "
        "không ```), bỏ trường nào không có dữ liệu, mảng rỗng nếu không có mục:\n"
        f"{extraction_json_skeleton()}\n\n"
        "===== VĂN BẢN ĐÃ CRAWL =====\n"
        f"{crawl_text}\n"
        "===== HẾT =====\n"
        "Nhắc lại: CHỈ JSON, chỉ FACTS có nguồn, ticker phải = " + ticker + "."
    )


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    # isolate the outermost JSON object
    i, j = s.find("{"), s.rfind("}")
    return s[i : j + 1] if i != -1 and j != -1 and j > i else s


def _run_claude(prompt: str, cfg: Config) -> tuple[str | None, str | None]:
    """Invoke `claude -p` headless; return (raw_text, error)."""
    cmd = [cfg.claude_bin, "-p", prompt, "--output-format", "json",
           "--model", cfg.claude_model]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=cfg.claude_timeout_s)
    except FileNotFoundError:
        return None, f"claude binary not found: {cfg.claude_bin}"
    except subprocess.TimeoutExpired:
        return None, f"claude timed out after {cfg.claude_timeout_s}s"
    if proc.returncode != 0:
        return None, f"claude exit {proc.returncode}: {(proc.stderr or '')[:300]}"
    out = proc.stdout.strip()
    # `--output-format json` wraps the result: {"type":"result","result":"<text>",...}
    try:
        wrapper = json.loads(out)
        if isinstance(wrapper, dict) and "result" in wrapper:
            return wrapper["result"], None
    except json.JSONDecodeError:
        pass
    return out, None  # already plain text


def _to_extraction(raw: str, ticker: str) -> tuple[Extraction | None, str | None]:
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        return None, f"JSON parse failed: {e}"
    data["ticker"] = ticker  # enforce
    data.setdefault("as_of", date.today().isoformat())
    try:
        return Extraction.model_validate(data), None
    except Exception as e:  # noqa: BLE001 - pydantic ValidationError et al.
        return None, f"schema validation failed: {type(e).__name__}: {e}"


def extract(crawl: CrawlResult, cfg: Config | None = None,
            crawl_text: str | None = None) -> tuple[Extraction | None, list[str]]:
    """Crawl result -> validated Extraction. One retry on parse/validation failure."""
    cfg = cfg or load_config()
    warnings: list[str] = []
    company = find_company(crawl.ticker) or {"ticker": crawl.ticker}
    text = crawl_text if crawl_text is not None else combined_text(crawl)
    if not text.strip():
        return None, ["no crawl text to extract from"]

    prompt = build_prompt(crawl.ticker, company, text)
    for attempt in range(2):
        raw, err = _run_claude(prompt, cfg)
        if err:
            warnings.append(f"claude attempt {attempt + 1}: {err}")
            continue
        ext, perr = _to_extraction(raw, crawl.ticker)
        if ext:
            ext.as_of = crawl.fetched_at
            return ext, warnings
        warnings.append(f"parse attempt {attempt + 1}: {perr}")
    return None, warnings
