# Review authority map

**Status:** accepted, 2026-10-08

This record answers one question for every Review decision: which code owns it. It also fixes the rule that later trust work must follow. That work covers validity explanations, a trust summary, incremental re-verification, external evidence, a richer signed record, promotion profiles, outcome history and earned autonomy.

## Decision

Each concern has exactly one authority. New trust capabilities are projections over these authorities or extensions of them. None of them adds a second Review state machine, policy engine, evidence model, history store, job queue or model client.

A change that creates a second authoritative home for a concern below is rejected unless the same change migrates the existing home and removes it.

## Rules

1. **Readiness decides.** `readiness.py` is the only code that computes whether a change may progress. Everything else contributes conditions to it.
2. **Projections derive.** A projection may group, explain or order state. It reads the authorities and stores nothing authoritative. Anything it caches can be rebuilt from them.
3. **AI is evidence, never authority.** AI review findings, narration and Project Knowledge statements can inform a reviewer. They never satisfy a human-judgment or required-check condition.
4. **Unknown is a state.** A missing, truncated, stale, unsupported or over-budget input yields `unknown`, or runs more work. It never yields a silent pass or a silent skip.
5. **Policy comes from the trusted base.** A policy edit on the head side of a change applies only to future changes, never to the change that makes it.

## Authority map

Paths are relative to `src/lemoncrow/pro/capabilities/review/`.

| Concern | Authority | Rule |
| --- | --- | --- |
| Immutable reviewed code | Snapshot (`ReviewRevision` in `session_models.py`, persisted by `store.py`) | Authoritative; never rewritten |
| Reviewable identity | `units.py` unit keys | Reuse |
| Human judgment validity | `revisions.py` (reconciliation, marks, frontier) | Reuse; explanations may be generalized |
| Evidence | `ReviewEvidence` in `session_models.py` | One evidence model |
| Typed check authority | `check_ingest.py` and its attestation classes | External verifiers feed it; they do not bypass it |
| Evidence freshness | `freshness.py` | Reuse |
| Runtime validation | `runtime_validation.py` and its execution records | Reuse |
| Review compute selection | `run_plan.py`, `run_plan_service.py`, `capability_catalog.py` | Extend; no second planner |
| Incremental test impact | `test_impact.py` | Extend |
| Cross-Snapshot check carry | `incremental.py` | Harden; never a silent carry |
| Shared base execution | `base_results.py` | Reuse |
| Trusted policy | `trusted_base.py` and `policy_generation` in `readiness.py` | Reuse |
| Readiness decision | `readiness.py` | Sole decision authority |
| AI review | `ai_review.py` | Evidence and context only |
| Author and reviewer independence | `independence.py`, `model_identity.py` | Reuse |
| Agent and model track record | `track_record.py` | Extend over Project Knowledge, not a new store |
| Signed Review Record | `review_record.py`, `record_signing.py`, `review_record_api.py` | Evolve compatibly; old records stay verifiable |
| Deployment and outcome facts | `outcomes/` (`deployments.py`, `deployment_outcomes.py`, `observation_outcomes.py`) | Facts feed track record and promotion reasoning |
| Change-set readiness | `change_sets.py` | A readiness condition, not a second decision |
| Workflow state (where the work stands) | `workflow_state.py` | Sole workflow-state authority |
| Next-action owner (who acts next) | hosted Review attention; locally, the Inbox inputs | Sole next-owner authority |
| Review ordering (what to open first) | `review_priority.py` | Sole ordering authority |
| Intelligence routes | `intelligence_policy.py` | Route authority for every AI call through the `internal_llm` facade; direct provider-client lanes (owned execution, agent sessions, the live reviewer, verified work, reranking, the coding runtime, the gateway proxy, embeddings, provider setup) are listed exceptions it does not govern |
| Background spend | `background_budget.py` | One spend ledger; hosted settles against the same contract |
| Project Knowledge | `project_knowledge.py` | Derived and rebuildable; never an authority |
| Product status | the capability status index, published as the [support matrix](../reference/support-matrix.md) | Sole present-tense statement of what ships |

## What each trust capability extends

| Trust capability | Extends |
| --- | --- |
| Validity explanations | `validity.py` (the reason vocabulary and its adapters) over `revisions.py`, `freshness.py`, `incremental.py`, `check_ingest.py` and `readiness.py` (explanation only; no new state) |
| Trust summary on a Review | `readiness.trust_section` on Review Status, over `readiness.py` and the Inbox row's `workflow_state.py` decision and next-action owner, which it copies through `register_review_api(review_workflow=...)` and never derives |
| Incremental re-verification | `incremental.py` (carry, including the executor environment), `test_impact.py` (selection and its coverage) and `run_plan.py`; the run plan's re-verification summary (`run_plan_reverification.py`) explains them and decides nothing |
| External evidence adapters | `evidence_adapters.py` (the adapter interface, its server-side grants and the in-toto/DSSE adapter) over `check_ingest.py`; an adapter produces only the classes its grant allows, and nothing `check_ingest.py` would not accept |
| Richer signed record | `review_record.py`, `record_signing.py` |
| Promotion profiles | `readiness.py` with another policy profile, plus `outcomes/` facts (shadow only until policy opts in) |
| Outcome-linked history | `track_record.py` reading Project Knowledge |
| Earned autonomy | `independence.py`, `track_record.py`, `auto_approval.py` (shadow evaluation first) |

## Consequences

- An automated inventory test checks that each primitive above exists and has a production caller, so the map cannot name a component that does not exist.
- A trust feature that needs state no row above can hold must first propose a new row and say which existing home it replaces.
- See [`lc review`](../reference/review.md) for the user-facing Review concepts.
