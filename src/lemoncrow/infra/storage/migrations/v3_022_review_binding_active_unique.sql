-- One active Review per logical subject is a database invariant, not a
-- convention of the binding code. Keep the newest active row for any subject
-- that raced into two before the index existed.
UPDATE review_bindings SET active = 0
 WHERE active = 1
   AND id NOT IN (
     SELECT id FROM (
       SELECT id, ROW_NUMBER() OVER (
         PARTITION BY repository_identity, subject_kind, subject_key
         ORDER BY updated_at DESC, id DESC
       ) AS rank_in_subject
         FROM review_bindings
        WHERE active = 1
     ) WHERE rank_in_subject = 1
   );
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_bindings_active_subject
  ON review_bindings(repository_identity, subject_kind, subject_key) WHERE active = 1;
