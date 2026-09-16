"""Real database acceptance: schema, repeatable import, chemistry and vector queries.

Self-test embeddings are synthetic and explicitly excluded from normal retrieval.
No server startup, no Docker, no LLM API calls.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from db.access import (ROOT, apply_schema, check_extensions, connect, digest, find_similar,
                       find_substructures, get_or_create_molecule, insert_fact,
                       search_embeddings, store_embedding, table_counts)
from scripts.migrate_runs_to_db import migrate


def emit(label, value):
    print(label, json.dumps(value, ensure_ascii=False, default=str), flush=True)


class RollbackChecks(Exception):
    pass


def expect_rejected(conn, label, statement, params=None, sqlstate='23514'):
    try:
        with conn.transaction():
            conn.execute(statement, params)
    except psycopg.Error as exc:
        assert exc.sqlstate == sqlstate, (label, exc.sqlstate, str(exc))
        emit('REJECTED', {'case':label,'sqlstate':exc.sqlstate})
    else:
        raise AssertionError(f'Invalid fact was accepted: {label}')


def verify_constraints(conn, run_id):
    before = table_counts(conn)
    try:
        with conn.transaction():
            invalid = 'NOT_A_VALID_SMILES!!!'
            molecule_id,error = get_or_create_molecule(conn,invalid)
            assert molecule_id is None and error
            proposal = insert_fact(conn,'proposals',{
                'run_id':run_id,'molecule_id':None,'round_no':0,'source_locator':'selfcheck:invalid',
                'raw_smiles':invalid,'parse_status':'invalid','parse_error':error,'raw_payload':{'smiles':invalid},
            },['run_id','source_locator'])
            row = conn.execute('SELECT raw_smiles,molecule_id,parse_error FROM aidd.proposals WHERE id=%s',
                               (proposal,)).fetchone()
            assert row['raw_smiles'] == invalid and row['molecule_id'] is None and row['parse_error']
            assert table_counts(conn)['molecules'] == before['molecules']
            emit('INVALID_SMILES', {'preserved_raw':True,'molecule_id':None,'no_fingerprint':True,'error':error})
            # Updating a fact is rejected in SQL, not just by a convention in Python.
            for table in before:
                expect_rejected(conn,table+':update',
                    f'UPDATE aidd.{table} SET created_at=created_at WHERE id=(SELECT min(id) FROM aidd.{table})',
                    sqlstate='55000')
                expect_rejected(conn,table+':delete',
                    f'DELETE FROM aidd.{table} WHERE id=(SELECT min(id) FROM aidd.{table})',sqlstate='55000')
            try:
                with conn.transaction():
                    conn.execute("INSERT INTO aidd.strategy_memories "
                                 "(memory_key,kind,content,content_sha256,trust_status,embedding) "
                                 "VALUES ('bad-embedding','selftest','x',%s,'selftest','[1,0,0]'::vector)",
                                 (digest('x'),))
            except psycopg.errors.CheckViolation:
                emit('EMBEDDING_METADATA', {'missing_metadata_rejected':True})
            else:
                raise AssertionError('Embedding without provenance was accepted')
            expect_rejected(conn,'invalid_smiles_without_error',
                "INSERT INTO aidd.proposals (run_id,round_no,source_locator,raw_smiles,parse_status) "
                "VALUES (%s,0,'selfcheck:no-error','bad','invalid')",(run_id,))
            expect_rejected(conn,'error_with_numeric_score',
                "INSERT INTO aidd.evaluations (proposal_id,molecule_id,evaluation_key,tool_name,status,value) "
                "SELECT proposal_id,molecule_id,'selfcheck:bad-score',tool_name,'error',0 FROM aidd.evaluations LIMIT 1")
            for label,dimension,checksum in [('wrong_dimension',4,digest('x')),('wrong_hash',3,'0'*64)]:
                expect_rejected(conn,label,
                    "INSERT INTO aidd.strategy_memories (memory_key,kind,content,content_sha256,trust_status,"
                    "embedding,embedding_model,embedding_version,embedding_dimension,embedding_space_key) "
                    "VALUES (%s,'selftest','x',%s,'selftest','[1,0,0]'::vector,'test','v1',%s,'test-space')",
                    (label,checksum,dimension))
            expect_rejected(conn,'artifact_without_hash',
                "INSERT INTO aidd.artifacts (artifact_key,run_id,artifact_type,uri,availability) "
                "VALUES ('selfcheck:bad-file',%s,'sdf','file:///selfcheck.sdf','present')",(run_id,))
            expect_rejected(conn,'noncanonical_structure',
                "INSERT INTO aidd.molecules (canonical_smiles) VALUES ('OCC')")
            with conn.transaction():
                conn.execute("SET LOCAL rdkit.morgan_fp_size=512")
                expect_rejected(conn,'wrong_fingerprint_width',
                    "INSERT INTO aidd.molecules (canonical_smiles) VALUES ('CCCCCCCCCCCCCCCCCCCCCCCCCCO')")
            conn.execute("SET LOCAL rdkit.morgan_fp_size=2048")
            new_id,new_error = get_or_create_molecule(conn,'OCC')
            assert new_id is not None and new_error is None
            new_mol = conn.execute('SELECT canonical_smiles,formula,size(mfp2) AS bits '
                                   'FROM aidd.molecules WHERE id=%s',(new_id,)).fetchone()
            assert new_mol == {'canonical_smiles':'CCO','formula':'C2H6O','bits':2048}
            emit('MOLECULE_WRITE',new_mol)
            vector_id,space = store_embedding(conn,'selfcheck:rollback-vector','Synthetic round-trip verification',
                [0.,0.,1.],'selftest','v1',kind='selftest',trust_status='selftest',metadata={'synthetic':True})
            nearest = search_embeddings(conn,[0.,0.,1.],'selftest','v1',space,limit=1,include_selftest=True)
            assert nearest[0]['id']==vector_id and nearest[0]['cosine_distance']==0
            emit('VECTOR_WRITE',{'new_row_retrieved':True,'cosine_distance':0,'will_roll_back':True})
            raise RollbackChecks()
    except RollbackChecks:
        pass
    assert table_counts(conn) == before
    emit('CONSTRAINT_TESTS', {'temporary_rows_rolled_back':True})


def verify_fresh_import(conn, source):
    before = table_counts(conn)
    try:
        with conn.transaction():
            first = migrate(conn,source,run_key='selfcheck:rollback-import')
            assert first['status']=='imported' and first['rows_added']['proposals']>0
            second = migrate(conn,source,run_key='selfcheck:rollback-import')
            assert second['status']=='already_imported' and not any(second['rows_added'].values())
            emit('FRESH_IMPORT',{'first':first,'second':second,'will_roll_back':True})
            raise RollbackChecks()
    except RollbackChecks:
        pass
    assert table_counts(conn)==before
    emit('FRESH_IMPORT_ROLLBACK',{'counts_unchanged':True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT/'runs_real')
    parser.add_argument('--smiles',help='Defaults to first imported candidate')
    parser.add_argument('--skip-schema',action='store_true',help='Verify existing tables without executing schema DDL')
    args = parser.parse_args()
    with connect() as conn:
        emit('CONNECTED',conn.execute('SELECT current_database() AS database,current_user AS role,'
                                     "current_setting('server_version') AS server_version").fetchone())
        emit('EXTENSIONS',check_extensions(conn))
        if not args.skip_schema:
            apply_schema(conn)
            apply_schema(conn)
        schema = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='aidd' "
                              "AND table_type='BASE TABLE' ORDER BY table_name").fetchall()
        assert len(schema) == 8
        vector_column = conn.execute("SELECT format_type(atttypid,atttypmod) AS column_type FROM pg_attribute "
                                     "WHERE attrelid='aidd.strategy_memories'::regclass AND attname='embedding'").fetchone()
        assert vector_column['column_type'] == 'vector'
        emit('SCHEMA',{'tables':[r['table_name'] for r in schema],
                       'repeat_ddl':'SKIPPED (existing tables)' if args.skip_schema else 'OK', **vector_column})
        emit('IMPORT_1',migrate(conn,args.source))
        before = table_counts(conn)
        second = migrate(conn,args.source)
        after = table_counts(conn)
        assert before == after and second['status'] == 'already_imported'
        emit('IMPORT_2',second)
        emit('IDEMPOTENT',{'unchanged_counts':True,'counts':after})
        run_id = second['run_id']
        query = args.smiles or conn.execute('SELECT raw_smiles FROM aidd.proposals WHERE run_id=%s '
                                           'AND molecule_id IS NOT NULL ORDER BY round_no,id LIMIT 1',
                                           (run_id,)).fetchone()['raw_smiles']
        hits = find_similar(conn,query,.4)
        # Cross-check GiST threshold results against an untruncated scalar predicate.
        baseline = conn.execute('SELECT id FROM aidd.molecules WHERE '
                                'tanimoto_sml(mfp2,morganbv_fp(mol_from_smiles(%s::cstring),2))>0.4',
                                (query,)).fetchall()
        assert {r['id'] for r in hits} == {r['id'] for r in baseline}
        assert hits and all(r['similarity'] > .4 for r in hits)
        emit('TANIMOTO',{'query':query,'threshold':'> 0.4','all_hits':hits,'matches_exact_scan':True})
        emit('SUBSTRUCTURE',{'smarts':'c1ccccc1','hit_count':len(find_substructures(conn,'c1ccccc1'))})
        chem = conn.execute('SELECT canonical_smiles,formula,molecular_weight,logp,size(mfp2) AS bits,'
                            'chemistry_profile FROM aidd.molecules WHERE id=%s',(hits[0]['id'],)).fetchone()
        emit('SERVER_CHEMISTRY',chem)
        verify_constraints(conn,run_id)
        verify_fresh_import(conn,args.source)
        fixtures = [('selfcheck-vector:1','Synthetic vector A',[1.,0.,0.]),
                    ('selfcheck-vector:2','Synthetic vector B',[.8,.2,0.]),
                    ('selfcheck-vector:3','Synthetic vector C',[0.,1.,0.])]
        ids = []
        for key,content,vector in fixtures:
            row_id,space = store_embedding(conn,key,content,vector,'selftest','v1',kind='selftest',
                                            trust_status='selftest',metadata={'synthetic':True})
            ids.append(row_id)
        count = table_counts(conn)
        for key,content,vector in fixtures:
            store_embedding(conn,key,content,vector,'selftest','v1',kind='selftest',
                            trust_status='selftest',metadata={'synthetic':True})
        assert table_counts(conn) == count
        found = search_embeddings(conn,[1.,0.,0.],'selftest','v1',space,limit=3,include_selftest=True)
        assert [r['id'] for r in found] == ids
        assert search_embeddings(conn,[1.,0.,0.],'selftest','v1',space) == []
        emit('VECTOR_COSINE',{'synthetic':True,'repeat_insert':'no duplicates','hits':found,
                              'excluded_from_normal_search':True})
        conn.execute('SET LOCAL enable_seqscan = off')
        plan = conn.execute("EXPLAIN (FORMAT JSON) SELECT id FROM aidd.strategy_memories "
                            "WHERE embedding_space_key='selftest-v1-3' "
                            "ORDER BY embedding::vector(3) <=> '[1,0,0]'::vector(3) LIMIT 3").fetchone()
        plan_text = json.dumps(plan)
        assert 'memories_selftest_cosine_hnsw' in plan_text
        emit('HNSW_PLAN',plan)
        emit('FINAL_COUNTS',table_counts(conn))
    print('SELF_CHECK PASSED (committed)',flush=True)


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(1)
