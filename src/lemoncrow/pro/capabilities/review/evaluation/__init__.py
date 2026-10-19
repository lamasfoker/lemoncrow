"""Review-system evaluation harness and Gate I (Phase 13 item 9).

A *corpus* of past Changes with known defects and known-clean Changes is
replayed per depth/preset (and Custom selection) through the real Review
pipeline -- capture, effective run plan, static analysis and AI review as
planned -- in a hermetic local mode. Model outputs come from recorded
fixtures: they are recorded once (synthetically, or live behind an explicit
flag and a hard budget cap) and replayed offline; a fixture whose prompt,
schema, model or case inputs no longer match is reported stale and never
reused.

The measured catch rate, noise, missed behavior deltas, cost and time per
selection feed Gate I (:mod:`.gate`): before a model, prompt, Review-policy
default or preset change ships, the corpus is replayed and compared with the
stored baseline under configured thresholds. See
``docs/reference/review-evaluation.md``.
"""
