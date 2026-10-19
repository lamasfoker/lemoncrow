-- Typed check/attestation authority on the existing ReviewEvidence plane.
ALTER TABLE review_evidence ADD COLUMN attestation_class TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN stable_key TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN provider TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN integration_id TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN provider_revision TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN branch TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN environment TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN attestation_scope TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN attested_by TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN remote_identity TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN observed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN waived_requirement_key TEXT NOT NULL DEFAULT '';
ALTER TABLE review_evidence ADD COLUMN execution_intent TEXT NOT NULL DEFAULT '';
