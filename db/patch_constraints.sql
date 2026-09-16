-- Additive repair for the existing eight tables. No table creation or deletion.
-- The same block is included at the end of schema.sql for fresh installations.
DO $patch$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM (VALUES
        ('molecules','molecules_fingerprint_contract',
         $c$CHECK (mfp2 IS NOT NULL AND size(mfp2)=2048 AND canonical_smiles=mol_to_smiles(mol)::text)$c$),
        ('proposals','proposals_error_contract',
         $c$CHECK ((parse_status='valid' AND parse_error IS NULL) OR
                   (parse_status='invalid' AND parse_error IS NOT NULL AND length(trim(parse_error))>0))$c$),
        ('proposals','proposals_id_molecule_key', $c$UNIQUE (id,molecule_id)$c$),
        ('evaluations','evaluations_proposal_molecule_fk',
         $c$FOREIGN KEY (proposal_id,molecule_id) REFERENCES aidd.proposals(id,molecule_id)$c$),
        ('evaluations','evaluations_score_contract',
         $c$CHECK ((status NOT IN ('error','skipped') OR value IS NULL) AND
                   (value IS NULL OR (value>'-Infinity'::float8 AND value<'Infinity'::float8)))$c$),
        ('runs','runs_mode_contract',
         $c$CHECK (execution_mode IN ('real','mock','unknown') AND
                   trust_status IN ('verified','unverified','historical_untrusted','selftest'))$c$),
        ('strategy_memories','memories_hash_contract',
         $c$CHECK (content_sha256=encode(sha256(convert_to(content,'UTF8')),'hex'))$c$),
        ('strategy_memories','memories_vector_contract',
         $c$CHECK ((embedding IS NULL AND embedding_model IS NULL AND embedding_version IS NULL
                    AND embedding_dimension IS NULL AND embedding_space_key='none') OR
                   (embedding IS NOT NULL AND embedding_model IS NOT NULL AND length(trim(embedding_model))>0
                    AND embedding_version IS NOT NULL AND length(trim(embedding_version))>0
                    AND embedding_dimension IS NOT NULL AND embedding_dimension=vector_dims(embedding)
                    AND embedding_space_key<>'none' AND vector_norm(embedding)>0))$c$),
        ('strategy_memories','memories_trust_contract',
         $c$CHECK (trust_status IN ('verified','unverified','historical_untrusted','selftest') AND
                   (embedding_space_key<>'selftest-v1-3' OR
                    (kind='selftest' AND trust_status='selftest' AND embedding_model='selftest'
                     AND embedding_version='v1' AND embedding_dimension=3 AND embedding IS NOT NULL)))$c$),
        ('artifacts','artifacts_file_contract',
         $c$CHECK (availability IN ('present','missing') AND length(trim(uri))>0
                   AND length(trim(artifact_type))>0 AND (size_bytes IS NULL OR size_bytes>=0)
                   AND (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$')
                   AND (availability<>'present' OR sha256 IS NOT NULL))$c$)
    ) AS specs(table_name,constraint_name,definition)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
                       WHERE conrelid=to_regclass('aidd.'||r.table_name) AND conname=r.constraint_name) THEN
            EXECUTE format('ALTER TABLE aidd.%I ADD CONSTRAINT %I %s',
                           r.table_name,r.constraint_name,r.definition);
        END IF;
    END LOOP;
END
$patch$;

-- Preserve immutable source facts, including protection from cascading deletion.
CREATE OR REPLACE FUNCTION aidd.reject_fact_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'append-only table %: % is not allowed; append a new fact',TG_TABLE_NAME,TG_OP
        USING ERRCODE='55000';
END
$$;
DO $patch$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['campaigns','runs','molecules','proposals','evaluations',
                             'filter_events','strategy_memories','artifacts'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger
                       WHERE tgrelid=to_regclass('aidd.'||t) AND tgname='immutable_fact_guard') THEN
            EXECUTE format('CREATE TRIGGER immutable_fact_guard BEFORE UPDATE OR DELETE ON aidd.%I
                            FOR EACH ROW EXECUTE FUNCTION aidd.reject_fact_mutation()',t);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger
                       WHERE tgrelid=to_regclass('aidd.'||t) AND tgname='immutable_truncate_guard') THEN
            EXECUTE format('CREATE TRIGGER immutable_truncate_guard BEFORE TRUNCATE ON aidd.%I
                            FOR EACH STATEMENT EXECUTE FUNCTION aidd.reject_fact_mutation()',t);
        END IF;
    END LOOP;
END
$patch$;
