-- ============================================================
-- AIDD 长期存储 schema（PostgreSQL 16 + RDKit cartridge + pgvector）
--
-- 本文件是 db/access.py、scripts/migrate_runs_to_db.py、scripts/selfcheck_db.py
-- 三处调用点的契约实现，遵循 docs/DATABASE_SETUP.md：
--
--   1. 事实表只追加：UPDATE 由数据库触发器直接拒绝（SQLSTATE 55000），
--      不靠 Python 侧的自觉。
--   2. 只有「有效且规范化」的分子进入 aidd.molecules；
--      无法解析的 SMILES 留在 aidd.proposals（raw_smiles + parse_error），molecule_id 为 NULL。
--   3. 结构相似度用 Tanimoto（mfp2），文本相似度用 embedding，
--      两套指标分开，不混用。
--   4. 化学计算全部在库内完成（生成列 + cartridge 函数），Python 侧不算分子。
--      mol / mfp2 / 描述符都是 GENERATED 列，调用方只需写入 canonical_smiles。
--   5. strategy_memories.embedding 有意不带维度：维度取决于选定的 embedding 模型。
--      带维度的 HNSW 索引按「嵌入空间」建成部分表达式索引，见文末。
--
-- 本文件可重复执行（全部 IF NOT EXISTS）。
-- ============================================================

CREATE SCHEMA IF NOT EXISTS aidd;

-- 本文件可被 psql 直接执行，也可被 psycopg 执行；统一设置好 search_path。
SET search_path TO aidd, public, pg_catalog;

-- Extensions must already exist; nothing is installed here.

-- ------------------------------------------------------------
-- 指纹位宽在本会话固定；访问层写入时也显式设定，CHECK 拒绝错误位宽。
-- rdkit.morgan_fp_size 的出厂默认是 512，而 db/access.py 的相似度查询
-- 显式按 2048 位计算；两边不一致会直接报
--   "All fingerprints should be the same length"
-- 自定义 GUC 只有在 cartridge 动态库被加载后才注册，所以先调用一次 rdkit_version()。
-- ------------------------------------------------------------
SELECT rdkit_version();
SET rdkit.morgan_fp_size = 2048;

-- ------------------------------------------------------------
-- 化学画像：记录「这一行的指纹是用什么算出来的」。
-- molecules.chemistry_profile 用它做默认值，换半径/位数/口径时指纹不会被误当成同一空间。
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION aidd.rdkit_chemistry_profile()
RETURNS jsonb
LANGUAGE sql
STABLE
AS $$
    SELECT jsonb_build_object(
        'rdkit_version',     rdkit_version(),
        'cartridge_version', (SELECT extversion FROM pg_extension WHERE extname = 'rdkit'),
        'policy',            'parse-canonical-v1-preserve-components-charge-stereo',
        'radius',            '2',
        'bits',              '2048'
    )
$$;

-- ------------------------------------------------------------
-- 只追加：任何对事实表的 UPDATE 都在 SQL 层被拒绝，而不是靠约定。
-- ------------------------------------------------------------
CREATE OR REPLACE FUNCTION aidd.reject_fact_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'append-only table %: UPDATE is not allowed (record a new fact instead)',
        TG_TABLE_NAME
        USING ERRCODE = '55000';
END
$$;

-- ------------------------------------------------------------
-- 1. campaigns：一次任务/战役的起源
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.campaigns (
    id           bigserial   PRIMARY KEY,
    campaign_key text        NOT NULL UNIQUE,
    name         text        NOT NULL,
    target_name  text,
    metadata     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 2. runs：每次跑批。同一 run_key 的不同 revision 各自成行，
--    supersedes_run_id 记录版本链；「达标」是版本化状态，不是永久标签。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.runs (
    id               bigserial   PRIMARY KEY,
    campaign_id      bigint      NOT NULL REFERENCES aidd.campaigns(id) ON DELETE CASCADE,
    run_key          text        NOT NULL,
    revision_hash    text        NOT NULL,
    supersedes_run_id bigint     REFERENCES aidd.runs(id) ON DELETE SET NULL,
    execution_mode   text        NOT NULL DEFAULT 'unknown',   -- real | mock | unknown
    trust_status     text        NOT NULL DEFAULT 'unverified', -- verified | unverified | historical_untrusted
    trust_reason     text,
    protocol_id      text,
    protocol         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    source_uri       text,
    source_summary   jsonb,
    round_records    jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_key, revision_hash)
);

CREATE INDEX IF NOT EXISTS runs_campaign_idx ON aidd.runs (campaign_id);
CREATE INDEX IF NOT EXISTS runs_protocol_idx ON aidd.runs (protocol_id);

-- ------------------------------------------------------------
-- 3. molecules：去重后的分子本体，只存一次。
--    调用方只写 canonical_smiles，其余全部由库内生成：
--      structure_key  = canonical SMILES（去重键）
--      mol / mfp2     = cartridge 原生类型（子结构检索 / Tanimoto 检索）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.molecules (
    id                bigserial PRIMARY KEY,
    canonical_smiles  text      NOT NULL,
    structure_key     text      GENERATED ALWAYS AS (canonical_smiles) STORED,
    mol               mol       GENERATED ALWAYS AS (mol_from_smiles(canonical_smiles::cstring)) STORED,
    mfp2              bfp       GENERATED ALWAYS AS (morganbv_fp(mol_from_smiles(canonical_smiles::cstring), 2)) STORED,
    inchikey          text      GENERATED ALWAYS AS (mol_inchikey(mol_from_smiles(canonical_smiles::cstring))) STORED,
    formula           text      GENERATED ALWAYS AS (mol_formula(mol_from_smiles(canonical_smiles::cstring))) STORED,
    molecular_weight  double precision GENERATED ALWAYS AS (mol_amw(mol_from_smiles(canonical_smiles::cstring))) STORED,
    logp              double precision GENERATED ALWAYS AS (mol_logp(mol_from_smiles(canonical_smiles::cstring))) STORED,
    tpsa              double precision GENERATED ALWAYS AS (mol_tpsa(mol_from_smiles(canonical_smiles::cstring))) STORED,
    hbd               integer   GENERATED ALWAYS AS (mol_hbd(mol_from_smiles(canonical_smiles::cstring))) STORED,
    hba               integer   GENERATED ALWAYS AS (mol_hba(mol_from_smiles(canonical_smiles::cstring))) STORED,
    rotatable_bonds   integer   GENERATED ALWAYS AS (mol_numrotatablebonds(mol_from_smiles(canonical_smiles::cstring))) STORED,
    rings             integer   GENERATED ALWAYS AS (mol_numrings(mol_from_smiles(canonical_smiles::cstring))) STORED,
    chemistry_profile jsonb     NOT NULL DEFAULT aidd.rdkit_chemistry_profile(),
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT molecules_structure_key_key UNIQUE (structure_key),
    CONSTRAINT molecules_parseable CHECK (mol IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS molecules_mol_gist  ON aidd.molecules USING gist (mol);
CREATE INDEX IF NOT EXISTS molecules_mfp2_gist ON aidd.molecules USING gist (mfp2);
CREATE INDEX IF NOT EXISTS molecules_inchikey  ON aidd.molecules (inchikey);

-- ------------------------------------------------------------
-- 4. proposals：谁在第几轮提出了什么。
--    同一分子可被多次提议 → 共享 molecule_id，各自保留 proposal_id。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.proposals (
    id                   bigserial   PRIMARY KEY,
    run_id               bigint      NOT NULL REFERENCES aidd.runs(id) ON DELETE CASCADE,
    molecule_id          bigint      REFERENCES aidd.molecules(id),
    round_no             integer     NOT NULL,
    source_locator       text        NOT NULL,   -- 来源定位：round 文件 + JSON 指针
    candidate_external_id text,
    raw_smiles           text        NOT NULL,   -- 原始文本，永不改写
    parse_status         text        NOT NULL,   -- valid | invalid
    parse_error          text,
    provider             text,
    model                text,
    rationale            text,
    focus_used           text,
    raw_payload          jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT proposals_source_key UNIQUE (run_id, source_locator),
    CONSTRAINT proposals_parse_status_check CHECK (parse_status IN ('valid', 'invalid')),
    -- 解析失败的提议不允许挂分子；有效的必须挂上。
    CONSTRAINT proposals_molecule_matches_parse CHECK (
        (parse_status = 'valid'   AND molecule_id IS NOT NULL) OR
        (parse_status = 'invalid' AND molecule_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS proposals_run_round_idx ON aidd.proposals (run_id, round_no);
CREATE INDEX IF NOT EXISTS proposals_molecule_idx  ON aidd.proposals (molecule_id);

-- ------------------------------------------------------------
-- 5. evaluations：每次工具评估的事实。
--    status=error（工具异常/超时等）与 status=invalid（分子无效）必须区分：
--    超时不能被记成「生物学失败」。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.evaluations (
    id             bigserial   PRIMARY KEY,
    proposal_id    bigint      NOT NULL REFERENCES aidd.proposals(id) ON DELETE CASCADE,
    molecule_id    bigint      REFERENCES aidd.molecules(id),
    evaluation_key text        NOT NULL UNIQUE,
    tool_name      text        NOT NULL,     -- validate | admet | dock | composite
    tool_version   text,
    protocol_id    text,
    parameters     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status         text        NOT NULL,     -- success | skipped | error | unknown
    value          double precision,
    unit           text,
    metrics        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    error          text,
    raw_payload    jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT evaluations_status_check CHECK (status IN ('success', 'skipped', 'error', 'unknown'))
);

CREATE INDEX IF NOT EXISTS evaluations_proposal_idx ON aidd.evaluations (proposal_id);
CREATE INDEX IF NOT EXISTS evaluations_tool_idx     ON aidd.evaluations (tool_name, status);

-- ------------------------------------------------------------
-- 6. filter_events：追加式的筛选记录（规则 + 阈值 + 理由 + 来源）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.filter_events (
    id           bigserial   PRIMARY KEY,
    proposal_id  bigint      NOT NULL REFERENCES aidd.proposals(id) ON DELETE CASCADE,
    evaluation_id bigint     REFERENCES aidd.evaluations(id),
    event_key    text        NOT NULL UNIQUE,
    stage        text        NOT NULL,     -- import_validation | historical_report | threshold ...
    rule_name    text        NOT NULL,
    rule_version text,
    decision     text        NOT NULL,     -- pass | reject | skip
    observed     jsonb       NOT NULL DEFAULT '{}'::jsonb,
    thresholds   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    reason       text,
    provenance   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT filter_events_decision_check CHECK (decision IN ('pass', 'reject', 'skip'))
);

CREATE INDEX IF NOT EXISTS filter_events_proposal_idx ON aidd.filter_events (proposal_id);

-- ------------------------------------------------------------
-- 7. strategy_memories：可检索的策略经验与文献。
--    trust_status 隔离历史/不可信数据，旧协议数据不混进当前检索。
--    embedding 不带维度；带向量时必须有完整出处（模型/版本/维度/空间），
--    由 CHECK 约束强制，而不是靠调用方自觉。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.strategy_memories (
    id                  bigserial   PRIMARY KEY,
    memory_key          text        NOT NULL,
    kind                text        NOT NULL DEFAULT 'strategy',  -- strategy | reflection | literature | selftest
    run_id              bigint      REFERENCES aidd.runs(id) ON DELETE SET NULL,
    round_no            integer,
    target_name         text,
    protocol_id         text,
    content             text        NOT NULL,
    content_sha256      text        NOT NULL,   -- 原文哈希：换模型时能对齐
    source_uri          text,
    trust_status        text        NOT NULL DEFAULT 'unverified', -- verified | unverified | historical_untrusted | selftest
    embedding           vector,
    embedding_model     text,
    embedding_version   text,
    embedding_dimension integer,
    embedding_space_key text        NOT NULL DEFAULT 'none',
    embedding_metadata  jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT strategy_memories_identity_key UNIQUE (memory_key, content_sha256, embedding_space_key),
    CONSTRAINT strategy_memories_embedding_provenance CHECK (
        embedding IS NULL
        OR (embedding_model IS NOT NULL AND embedding_version IS NOT NULL
            AND embedding_dimension IS NOT NULL AND embedding_dimension > 0
            AND embedding_space_key IS NOT NULL AND embedding_space_key <> 'none')
    )
);

CREATE INDEX IF NOT EXISTS strategy_memories_run_idx   ON aidd.strategy_memories (run_id);
CREATE INDEX IF NOT EXISTS strategy_memories_trust_idx ON aidd.strategy_memories (trust_status);

-- ------------------------------------------------------------
-- 8. artifacts：大文件只存指针（URI + SHA256 + 类型 + 大小 + 是否仍在），
--    本体留在文件系统，绝不把姿势文件塞进 bytea。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aidd.artifacts (
    id            bigserial   PRIMARY KEY,
    artifact_key  text        NOT NULL UNIQUE,
    run_id        bigint      REFERENCES aidd.runs(id) ON DELETE CASCADE,
    evaluation_id bigint      REFERENCES aidd.evaluations(id) ON DELETE SET NULL,
    artifact_type text        NOT NULL,      -- source_json | receptor | pose | ...
    uri           text        NOT NULL,
    sha256        text,
    size_bytes    bigint,
    mime_type     text,
    availability  text        NOT NULL DEFAULT 'present',  -- present | missing
    metadata      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artifacts_run_idx ON aidd.artifacts (run_id);

-- ------------------------------------------------------------
-- 只追加触发器：事实一旦写入就不允许原地修改。
-- 可信状态变更也追加新事实，不覆盖原记录。
-- ------------------------------------------------------------
-- Append-only guards are installed by the additive contract block below.

-- ------------------------------------------------------------
-- 向量索引：embedding 列刻意不带维度，所以 HNSW 必须按「嵌入空间」建部分表达式索引。
-- 真实模型接入后，用 db.access.create_embedding_index(conn, space, dimension) 按空间建；
-- 下面这个 3 维索引只服务于 selfcheck 的合成自测向量（trust_status='selftest'），
-- 所以用部分索引把它和真实检索空间隔开，自测数据不会混进正常检索。
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS memories_selftest_cosine_hnsw
    ON aidd.strategy_memories USING hnsw ((embedding::vector(3)) vector_cosine_ops)
    WHERE embedding_space_key = 'selftest-v1-3';

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

RESET search_path;
