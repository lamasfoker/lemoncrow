#!/usr/bin/env python3
"""Fail closed when the canonical lexical release result regresses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

BASELINE_MRR = 0.642478  # published as 0.6425 in BENCHMARKS.md
BASELINE_P95_MS = 141.0
BASELINE_N = 6292
EXPECTED_GOLD_SHA256 = {
    "benchmarks/codebench/data/bench_pairs_def_gold.json": "be54e2e17ab022678fdfb1245e4e588281c01738f9a4caad6757e23ad9422aef",
    "benchmarks/codebench/data/bench_pairs_content_gold.json": "675523dda0e6ad282bef2ee0f59dd5a9ef8fe082d56d89605f2f5cde3f25b035",
    "benchmarks/codebench/data/bench_pairs_semantic_gold.json": "bec31f0ae1a954eaa4ccc97ae92301b9e4f9bb0b73717cea48f6663586545b4b",
    "benchmarks/codebench/data/bench_pairs_swebench_gold.json": "0f0c059562697724252a27c523c14bdf4f5f54d8a8268a48cc469cc62110ed17",
    "benchmarks/codebench/data/bench_pairs_sessions_gold.json": "8c9898aab2da968ee467594b5fe7adcc06abab1bd40d19ee1c0fb6a80408b747",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "result", type=Path, help="JSON output from `lemoncrow eval retrieval --channel lexical --full --json`"
    )
    ap.add_argument("--min-mrr", type=float, default=BASELINE_MRR)
    ap.add_argument("--max-p95-ms", type=float, default=BASELINE_P95_MS)
    args = ap.parse_args()
    raw = json.loads(args.result.read_text())
    row = raw.get("lexical", raw)
    golds = row.get("golds") or {}
    if not golds:
        raise SystemExit("retrieval result has no gold breakdown")
    if row.get("gold_sha256") != EXPECTED_GOLD_SHA256:
        raise SystemExit("retrieval corpus digest set does not match the frozen release corpus")
    n = sum(int(v.get("n", 0)) for v in golds.values())
    if n != BASELINE_N:
        raise SystemExit(f"retrieval corpus changed: expected {BASELINE_N} scored cases, got {n}")
    mrr = sum(float(v.get("mrr", 0.0)) * int(v.get("n", 0)) for v in golds.values()) / n
    p95 = float((row.get("latency_ms") or {}).get("p95", 0.0))
    result = {
        "ok": mrr + 1e-9 >= args.min_mrr and p95 <= args.max_p95_ms,
        "mrr": round(mrr, 6),
        "min_mrr": args.min_mrr,
        "p95_ms": p95,
        "max_p95_ms": args.max_p95_ms,
        "n": n,
    }
    print(json.dumps(result, sort_keys=True))
    if mrr + 1e-9 < args.min_mrr:
        raise SystemExit(f"MRR regression: {mrr:.6f} < {args.min_mrr:.6f}")
    if p95 > args.max_p95_ms:
        raise SystemExit(f"latency regression: p95 {p95:.1f}ms > {args.max_p95_ms:.1f}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
