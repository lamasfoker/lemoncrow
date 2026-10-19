-- Phase 8: reviewer disposition for AI findings.
-- This extends the existing Annotation trust/provenance model; it does not create
-- another finding or human-judgment store.
ALTER TABLE annotations ADD COLUMN finding_disposition TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN finding_disposition_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN finding_disposition_revision_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN finding_disposition_reviewer_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN finding_disposition_evidence_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE annotation_versions ADD COLUMN finding_disposition TEXT NOT NULL DEFAULT '';
ALTER TABLE annotation_versions ADD COLUMN finding_disposition_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE annotation_versions ADD COLUMN finding_disposition_revision_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotation_versions ADD COLUMN finding_disposition_reviewer_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotation_versions ADD COLUMN finding_disposition_evidence_json TEXT NOT NULL DEFAULT '[]';
