-- Executor trust class and verification statement of execution-derived
-- evidence (Phase 9R items 36, 37).
ALTER TABLE review_evidence ADD COLUMN executor_class TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN execution_verification TEXT NOT NULL DEFAULT '';
