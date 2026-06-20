#!/usr/bin/env python3
"""Summarize logs/llm_cost.jsonl into per-ticker (tag) + per-model cost/token totals.

Each line is one `claude` CLI call logged by the claude_code Tier-2 client
(cost_usd is API-equivalent; $0 marginal under a Max plan). Group by the
CPKG_COST_TAG the runner set per ticker.

Usage:
  .venv/bin/python bin/llm_cost_report.py            # all rows
  .venv/bin/python bin/llm_cost_report.py --since 2026-06-20   # ISO date/time filter
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOG = REPO_ROOT / "logs" / "llm_cost.jsonl"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="only rows with ts >= this ISO prefix")
    ap.add_argument("--file", default=str(LOG))
    args = ap.parse_args()

    p = Path(args.file)
    if not p.is_file():
        print(f"no cost log yet: {p}")
        return 1

    # by[tag][model] = [calls, cost, in_tok, out_tok]
    by: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0, 0]))
    rows = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if args.since and (r.get("ts") or "") < args.since:
            continue
        rows += 1
        agg = by[r.get("tag") or "(untagged)"][r.get("model") or "?"]
        agg[0] += 1
        agg[1] += float(r.get("cost_usd") or 0)
        agg[2] += int(r.get("input_tokens") or 0)
        agg[3] += int(r.get("output_tokens") or 0)

    if not rows:
        print("no rows matched")
        return 1

    grand = [0, 0.0, 0, 0]
    print(f"{'ticker':<12}{'model':<22}{'calls':>7}{'cost_usd':>11}{'in_tok':>10}{'out_tok':>10}")
    print("-" * 72)
    for tag in sorted(by):
        sub = [0, 0.0, 0, 0]
        for model in sorted(by[tag]):
            c, cost, it, ot = by[tag][model]
            print(f"{tag:<12}{model:<22}{c:>7}{cost:>11.4f}{it:>10}{ot:>10}")
            for i, v in enumerate((c, cost, it, ot)):
                sub[i] += v
                grand[i] += v
        if len(by[tag]) > 1:
            print(f"{tag:<12}{'  └ subtotal':<22}{sub[0]:>7}{sub[1]:>11.4f}{sub[2]:>10}{sub[3]:>10}")
        print()
    print("-" * 72)
    print(f"{'TOTAL':<12}{'':<22}{grand[0]:>7}{grand[1]:>11.4f}{grand[2]:>10}{grand[3]:>10}")
    n_tags = len([t for t in by if t != "(untagged)"])
    if n_tags:
        print(f"\nper-ticker average: ${grand[1] / n_tags:.4f}  (over {n_tags} tickers, API-equivalent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
