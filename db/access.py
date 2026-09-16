"""Synchronous psycopg 3 access. No Python chemistry and no service management."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
TABLES = ('campaigns', 'runs', 'molecules', 'proposals', 'evaluations',
          'filter_events', 'strategy_memories', 'artifacts')


def digest(value):
    data = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                         separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


def connect():
    load_dotenv(ROOT / '.env')
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        raise RuntimeError('DATABASE_URL is missing; set it in the local .env')
    try:
        return psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row,
                               application_name='aidd-migration')
    except psycopg.OperationalError:
        raise RuntimeError('PostgreSQL connection failed. Check the existing instance; '
                           'if stopped, use D:\\AIDD-PostgreSQL\\启动PostgreSQL.bat. '
                           'This script never starts a database server.') from None


def check_extensions(conn):
    rows = conn.execute("SELECT e.extname,e.extversion,n.nspname FROM pg_extension e "
                        "JOIN pg_namespace n ON n.oid=e.extnamespace "
                        "WHERE e.extname IN ('rdkit','vector') ORDER BY e.extname").fetchall()
    if {r['extname'] for r in rows} != {'rdkit', 'vector'}:
        raise RuntimeError('Existing rdkit and vector extensions are required; nothing was installed')
    namespaces = list(dict.fromkeys(['pg_catalog', *(r['nspname'] for r in rows), 'aidd']))
    conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(
        sql.SQL(',').join(map(sql.Identifier, namespaces))))
    return rows


def apply_schema(conn):
    with conn.transaction():
        check_extensions(conn)
        conn.execute((ROOT / 'db/schema.sql').read_text(encoding='utf-8'), prepare=False)
    return table_counts(conn)


def table_counts(conn):
    return {name: conn.execute(sql.SQL('SELECT count(*) AS n FROM aidd.{}').format(
        sql.Identifier(name))).fetchone()['n'] for name in TABLES}


def insert_fact(conn, table, values, conflict_columns):
    """Insert or return an identical existing fact. Never update a row."""
    if table not in TABLES:
        raise ValueError('Unknown fact table')
    columns = list(values)
    params = [Jsonb(v) if isinstance(v, (dict, list)) else v for v in values.values()]
    statement = sql.SQL('INSERT INTO aidd.{} ({}) VALUES ({}) ON CONFLICT ({}) '
                        'DO NOTHING RETURNING id').format(
        sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)),
        sql.SQL(',').join(sql.Placeholder() for _ in columns),
        sql.SQL(',').join(map(sql.Identifier, conflict_columns)))
    row = conn.execute(statement, params).fetchone()
    if row:
        return row['id']
    conditions = sql.SQL(' AND ').join(sql.SQL('{} IS NOT DISTINCT FROM %s').format(
        sql.Identifier(k)) for k in conflict_columns)
    existing = conn.execute(sql.SQL('SELECT * FROM aidd.{} WHERE {}').format(
        sql.Identifier(table), conditions), [values[k] for k in conflict_columns]).fetchone()
    # Enforce idempotency rather than silently accepting a conflicting payload.
    for key, value in values.items():
        if existing[key] != value:
            raise ValueError(f'Conflicting immutable fact in {table}, field {key}')
    return existing['id']


def get_or_create_molecule(conn, raw_smiles):
    """Return (id, error); invalid strings never get fingerprints."""
    try:
        with conn.transaction():  # savepoint protects the enclosing import
            conn.execute("SELECT set_config('rdkit.morgan_fp_size','2048',true)")
            canonical = conn.execute('SELECT mol_to_smiles(mol_from_smiles(%s::cstring)) AS smi',
                                     (raw_smiles,)).fetchone()['smi']
            if canonical is None:
                return None, 'mol_from_smiles returned NULL: invalid SMILES'
            row = conn.execute('INSERT INTO aidd.molecules (canonical_smiles) VALUES (%s) '
                               'ON CONFLICT (structure_key) DO NOTHING RETURNING id',
                               (canonical,)).fetchone()
            if row:
                return row['id'], None
            row = conn.execute("SELECT id FROM aidd.molecules WHERE canonical_smiles=%s "
                               "AND chemistry_profile->>'rdkit_version'=rdkit_version() "
                               "AND chemistry_profile->>'cartridge_version'="
                               "(SELECT extversion FROM pg_extension WHERE extname='rdkit') "
                               "AND chemistry_profile->>'policy'='parse-canonical-v1-preserve-components-charge-stereo' "
                               "AND chemistry_profile->>'radius'='2' AND chemistry_profile->>'bits'='2048'",
                               (canonical,)).fetchone()
            if not row:
                raise RuntimeError('Molecule key conflict did not resolve to an identical structure')
            return row['id'], None
    except psycopg.DataError as exc:
        return None, f'{exc.sqlstate}: {exc.diag.message_primary}'


def find_similar(conn, smiles, threshold=0.4):
    """Exact Tanimoto threshold search; no LIMIT and no approximate index."""
    if not 0 <= threshold <= 1:
        raise ValueError('threshold must be in [0,1]')
    with conn.transaction():
        conn.execute("SELECT set_config('rdkit.morgan_fp_size','2048',true)")
        conn.execute("SELECT set_config('rdkit.tanimoto_threshold',%s,true)", (str(threshold),))
        return conn.execute('WITH q AS (SELECT morganbv_fp(mol_from_smiles(%s::cstring),2) AS fp) '
                            'SELECT m.id,m.canonical_smiles,tanimoto_sml(m.mfp2,q.fp) AS similarity '
                            'FROM aidd.molecules m CROSS JOIN q '
                            'WHERE m.mfp2 %% q.fp AND tanimoto_sml(m.mfp2,q.fp)>%s '
                            'ORDER BY similarity DESC,m.id', (smiles, threshold)).fetchall()


def find_substructures(conn, smarts, use_chirality=True):
    with conn.transaction():
        conn.execute("SELECT set_config('rdkit.do_chiral_sss',%s,true)",
                     ('true' if use_chirality else 'false',))
        return conn.execute('SELECT id,canonical_smiles FROM aidd.molecules '
                            'WHERE mol @> %s::qmol ORDER BY id', (smarts,)).fetchall()


def _vector_text(vector):
    values = [float(v) for v in vector]
    if not values or not all(math.isfinite(v) for v in values) or not any(values):
        raise ValueError('Cosine embedding must be finite, nonempty and nonzero')
    return '[' + ','.join(map(str, values)) + ']'


def store_embedding(conn, memory_key, content, vector, model, version, *,
                    kind='strategy', trust_status='unverified', metadata=None):
    text = _vector_text(vector)
    if not model or not version:
        raise ValueError('Embedding model and version are required')
    dimension = len(vector)
    metadata = metadata or {}
    space = ('selftest-v1-3' if kind == 'selftest' and model == 'selftest' and version == 'v1'
             and dimension == 3 else digest([model, version, dimension, metadata]))
    conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('embedding:' + space,))
    existing = conn.execute('SELECT id,embedding::text AS embedding FROM aidd.strategy_memories '
                            'WHERE memory_key=%s AND content_sha256=%s AND embedding_space_key=%s',
                            (memory_key, digest(content), space)).fetchone()
    if existing:
        if json.loads(existing['embedding']) != json.loads(
                conn.execute('SELECT %s::vector::text AS v', (text,)).fetchone()['v']):
            raise ValueError('Existing embedding differs; append a new model/config version')
        return existing['id'], space
    row = conn.execute('INSERT INTO aidd.strategy_memories '
                       '(memory_key,kind,content,content_sha256,trust_status,embedding,embedding_model,'
                       'embedding_version,embedding_dimension,embedding_space_key,embedding_metadata) '
                       'VALUES (%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s) RETURNING id',
                       (memory_key,kind,content,digest(content),trust_status,text,model,version,
                        dimension,space,Jsonb(metadata))).fetchone()
    return row['id'], space


def create_embedding_index(conn, space, dimension):
    # vector HNSW supports <=2000 dimensions; no silent halfvec conversion.
    if not isinstance(dimension, int) or not 1 <= dimension <= 2000:
        raise ValueError('vector HNSW requires a dimension from 1 to 2000')
    index = 'memory_cosine_' + digest(space)[:16]
    conn.execute(sql.SQL('CREATE INDEX IF NOT EXISTS {} ON aidd.strategy_memories '
                         'USING hnsw ((embedding::vector({})) vector_cosine_ops) '
                         'WHERE embedding_space_key={}').format(
        sql.Identifier(index), sql.Literal(dimension), sql.Literal(space)))


def search_embeddings(conn, vector, model, version, space, *, limit=5, include_selftest=False):
    text = _vector_text(vector)
    dimension = len(vector)
    if not 1 <= limit <= 1000:
        raise ValueError('limit must be from 1 to 1000')
    trusted = ('verified','unverified','selftest') if include_selftest else ('verified','unverified')
    with conn.transaction():
        conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order'")
        return conn.execute(sql.SQL(
            'SELECT id,content,embedding_model,embedding_version,embedding_dimension,'
            'embedding::vector({}) <=> %s::vector({}) AS cosine_distance '
            'FROM aidd.strategy_memories WHERE embedding_space_key={} '
            'AND embedding_model=%s AND embedding_version=%s AND embedding_dimension=%s '
            'AND trust_status=ANY(%s) ORDER BY embedding::vector({}) <=> %s::vector({}) LIMIT %s'
        ).format(sql.Literal(dimension),sql.Literal(dimension),sql.Literal(space),
                 sql.Literal(dimension),sql.Literal(dimension)),
            (text,model,version,dimension,list(trusted),text,limit)).fetchall()
