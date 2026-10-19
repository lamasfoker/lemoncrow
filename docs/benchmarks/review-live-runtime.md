# Review live-runtime latency baseline

**Measured:** 2026-09-23  
**Commit:** `bfac890da760`  
**Harness:** `benchmarks/review_live_runtime.py`  
**Fixture:** 40-file Git repository, 8 initially changed files, 3 isolated samples. Each sample starts a fresh loopback LemonCrow server and fresh repository/state directory.

This is a directional developer-loop baseline, **not an SLO**. Wall-clock values vary by machine, filesystem cache, process scheduling, and repository shape. Compare the same harness/fixture across commits rather than turning these numbers into CI pass/fail thresholds.

| Product boundary | Median | p95 | What is measured |
| --- | ---: | ---: | --- |
| Warm `lc review --open` navigation | **6.24 ms** | **7.47 ms** | CLI invocation start → `webbrowser.open()` after exact prepared-source binding validation and local browser pairing. |
| Source-ready first diff | **549.22 ms** | **715.12 ms** | Progressive working-tree capture start → exact source + stable Review targets persisted and Reader-ready callback fired. Impact enrichment continues afterwards. |
| Full initial capture | **623.87 ms** | **776.85 ms** | Same capture start → background impact enrichment/finalization complete. |
| Reader checkpoint capture | **181.65 ms** | **204.60 ms** | Source edit → Reader-equivalent `POST /api/reviews/{id}/refresh` returns the next immutable Review revision. |
| Feedback handoff acceptance | **0.137 ms** | **0.828 ms** | Exact-session feedback adapter start → active OpenCode session inbox accepts the message. This intentionally excludes host/model turnaround. |

The median source-ready boundary precedes full capture completion by about **74.7 ms** on this fixture. The more important UX result is that warm navigation does not rebuild the Review before opening: it reaches browser navigation in single-digit milliseconds on these samples.

## Raw runs

| Run | First diff | Full capture | Warm navigation | Checkpoint | Handoff accept |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 549.22 ms | 623.87 ms | 7.61 ms | 181.65 ms | 0.904 ms |
| 2 | 733.56 ms | 793.85 ms | 5.21 ms | 175.64 ms | 0.128 ms |
| 3 | 468.12 ms | 530.86 ms | 6.24 ms | 207.15 ms | 0.137 ms |

## Reproduce

```bash
uv run python benchmarks/review_live_runtime.py \
  --samples 3 \
  --files 40 \
  --changed-files 8
```

Use `--json` for machine-readable output. The benchmark requires `frontend/dist` and starts the repository's local Review server with trusted local filesystem refresh enabled so the checkpoint measurement follows the same `/refresh` path as Reader.
