"""Append-only import of round_*.json and summary.json; never runs chemistry in Python.

python scripts/migrate_runs_to_db.py --source runs_real
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.access import ROOT, apply_schema, check_extensions, connect, digest, get_or_create_molecule, insert_fact, table_counts


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def raw_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def insert_artifact(conn, run_key, run_id, path, artifact_type='source_json', evaluation_id=None):
    path = Path(path).resolve()
    # Do not read arbitrary external paths or download URIs found in imported JSON.
    present = path.is_relative_to(ROOT) and path.is_file()
    checksum = file_sha(path) if present else None
    values = {
        'artifact_key': digest([run_key, evaluation_id, path.as_uri(), checksum]),
        'artifact_type': artifact_type, 'uri': path.as_uri(), 'sha256': checksum,
        'mime_type': 'application/json' if path.suffix == '.json' else 'chemical/x-pdbqt' if path.suffix == '.pdbqt' else 'application/octet-stream',
        'size_bytes': path.stat().st_size if present else None,
        'availability': 'present' if present else 'missing',
        'metadata': {'hash_verified': present},
        **({'evaluation_id': evaluation_id} if evaluation_id else {'run_id': run_id}),
    }
    return insert_fact(conn, 'artifacts', values, ['artifact_key'])


def import_proposal(conn, run_id, run_version_key, round_no, locator, candidate, focus_used, protocol_id):
    smi = raw_text(candidate.get('smiles', ''))
    molecule_id, parse_error = get_or_create_molecule(conn, smi)
    proposal_id = insert_fact(conn, 'proposals', {
        'run_id': run_id, 'molecule_id': molecule_id, 'round_no': round_no,
        'source_locator': locator, 'candidate_external_id': candidate.get('candidate_id'),
        'raw_smiles': smi, 'parse_status': 'valid' if molecule_id is not None else 'invalid',
        'parse_error': parse_error, 'provider': candidate.get('provider'),
        'model': candidate.get('model'), 'rationale': candidate.get('rationale'),
        'focus_used': focus_used, 'raw_payload': candidate,
    }, ['run_id', 'source_locator'])
    insert_fact(conn, 'filter_events', {
        'proposal_id': proposal_id, 'event_key': digest([run_version_key,locator,'db_parse']),
        'stage': 'import_validation', 'rule_name': 'cartridge_parse', 'rule_version': 'v1',
        'decision': 'pass' if molecule_id is not None else 'reject',
        'observed': {'parseable': molecule_id is not None}, 'thresholds': {},
        'reason': parse_error or 'Parsed by the installed RDKit cartridge',
        'provenance': {'method': 'database', 'historical_filter': False},
    }, ['event_key'])
    for tool in ('validate', 'admet', 'dock', 'composite'):
        if tool == 'composite':
            if 'composite_score' not in candidate:
                continue
            payload = {'score': candidate['composite_score']}
        else:
            payload = candidate.get(tool)
            if not isinstance(payload, dict):
                continue
        score = payload.get('score') if tool in ('dock', 'composite') else payload.get('summary_score') if tool == 'admet' else None
        error = payload.get('error')
        if payload.get('status') == 'disabled' or (error and ('disabled' in str(error) or 'skipped' in str(error))):
            status = 'skipped'
        elif error or payload.get('valid') is False or (tool in ('dock', 'composite') and not finite(score)):
            status = 'error' if error or payload.get('valid') is False else 'unknown'
        else:
            status = 'success'
        evaluation_id = insert_fact(conn, 'evaluations', {
            'proposal_id': proposal_id, 'molecule_id': molecule_id,
            'evaluation_key': digest([run_version_key,locator,tool]),
            'tool_name': tool, 'tool_version': payload.get('version'),
            'protocol_id': candidate.get('protocol_id') or protocol_id,
            'parameters': payload.get('provenance') or {}, 'status': status,
            'value': float(score) if finite(score) and status == 'success' else None,
            'unit': 'kcal/mol' if tool == 'dock' else None,
            'metrics': payload, 'error': str(error) if error else None, 'raw_payload': payload,
        }, ['evaluation_key'])
        if tool == 'validate' and isinstance(payload.get('lipinski_pass'), bool):
            insert_fact(conn, 'filter_events', {
                'proposal_id': proposal_id, 'evaluation_id': evaluation_id,
                'event_key': digest([run_version_key,locator,'reported_lipinski']),
                'stage': 'historical_report', 'rule_name': 'reported_lipinski',
                'rule_version': None, 'decision': 'pass' if payload['lipinski_pass'] else 'reject',
                'observed': {'lipinski_pass': payload['lipinski_pass']},
                'thresholds': {}, 'reason': 'Preserved source decision; original thresholds unspecified',
                'provenance': {'source_locator': locator, 'recomputed': False},
            }, ['event_key'])
        for key, uri in (payload.get('artifacts') or {}).items():
            if key != 'directory' and isinstance(uri, str) and '://' not in uri:
                p = Path(uri)
                if not p.is_absolute():
                    p = ROOT / p
                insert_artifact(conn, run_version_key, run_id, p, key, evaluation_id)
    return proposal_id


def migrate(conn, source, run_key=None):
    source = Path(source).resolve()
    summary_path = source / 'summary.json'
    paths = sorted(source.glob('round_*.json'), key=lambda p: int(p.stem.split('_')[-1]))
    if not summary_path.is_file() or not paths:
        raise ValueError('Source must contain summary.json and round_<number>.json files')
    def load(path):
        return json.loads(path.read_text(encoding='utf-8-sig'),
                          parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f'Non-finite JSON number: {token}')))
    summary = load(summary_path)
    records = {p.name: load(p) for p in paths}
    numbers = [r['round'] for r in records.values()]
    if any(not isinstance(n, int) or n < 0 for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError('Round numbers must be unique nonnegative integers')
    run_key = run_key or summary.get('run_id') or 'directory:' + source.as_uri()
    revision = digest([(p.name, file_sha(p)) for p in [summary_path, *paths]])
    version_key = digest([run_key, revision])
    with conn.transaction():
        check_extensions(conn)
        conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (run_key,))
        existing = conn.execute('SELECT id FROM aidd.runs WHERE run_key=%s AND revision_hash=%s',
                                (run_key,revision)).fetchone()
        if existing:
            return {'status':'already_imported','run_id':existing['id'],'revision_hash':revision,
                    'rows_added':{t:0 for t in table_counts(conn)}}
        before = table_counts(conn)
        campaign_id = insert_fact(conn, 'campaigns', {
            'campaign_key': 'import:' + digest(run_key), 'name': source.name,
            'target_name': summary.get('target'), 'metadata': {'import_run_key': run_key},
        }, ['campaign_key'])
        previous = conn.execute('SELECT id FROM aidd.runs WHERE run_key=%s ORDER BY id DESC LIMIT 1',
                                (run_key,)).fetchone()
        explicit_mock = summary.get('is_mock')
        mode = 'mock' if explicit_mock is True else 'real' if explicit_mock is False else 'unknown'
        protocol_id = summary.get('protocol_id')
        trust = 'historical_untrusted' if not protocol_id else 'unverified'
        run_id = insert_fact(conn, 'runs', {
            'campaign_id':campaign_id,'run_key':run_key,'revision_hash':revision,
            'supersedes_run_id':previous['id'] if previous else None,
            'execution_mode':mode,'trust_status':trust,
            'trust_reason':'Imported source evidence only; no protocol or scientific validation inferred',
            'protocol_id':protocol_id,'protocol':summary.get('protocol') or {},
            'source_uri':source.as_uri(),'source_summary':summary,'round_records':records,
        }, ['run_key','revision_hash'])
        for path in [summary_path,*paths]:
            insert_artifact(conn, version_key, run_id, path)
        for filename, record in records.items():
            round_no = record['round']
            candidates = record.get('candidates', [])
            represented = Counter((c.get('provider'),c.get('model'),raw_text(c.get('smiles',''))) for c in candidates)
            for index,candidate in enumerate(candidates):
                import_proposal(conn,run_id,version_key,round_no,f'{filename}#/candidates/{index}',
                                candidate,record.get('focus_used'),record.get('protocol_id') or protocol_id)
            # Preserve generated items that never reached the candidates array, including invalid input.
            for gi, generated in enumerate(record.get('generator_outputs', [])):
                for si,smi in enumerate(generated.get('smiles_list') or []):
                    key = (generated.get('provider'),generated.get('model'),raw_text(smi))
                    if represented[key]:
                        represented[key] -= 1
                        continue
                    candidate = {'smiles':smi,'provider':generated.get('provider'),
                                 'model':generated.get('model'),'rationale':generated.get('rationale')}
                    import_proposal(conn,run_id,version_key,round_no,
                                    f'{filename}#/generator_outputs/{gi}/smiles_list/{si}',candidate,
                                    record.get('focus_used'),record.get('protocol_id') or protocol_id)
            judgment = record.get('judgment') or {}
            for field,content,kind in [('focus',record.get('focus_next') or record.get('focus'),'strategy'),
                                       ('reflection',judgment.get('reflection'),'reflection')]:
                if not isinstance(content,str) or not content.strip():
                    continue
                insert_fact(conn,'strategy_memories',{
                    'memory_key':digest([version_key,filename,field]),'kind':kind,'run_id':run_id,
                    'round_no':round_no,'target_name':record.get('target') or summary.get('target'),
                    'protocol_id':record.get('protocol_id') or protocol_id,
                    'content':content,'content_sha256':digest(content),'source_uri':(source/filename).as_uri(),
                    'trust_status':trust,'embedding_space_key':'none',
                },['memory_key','content_sha256','embedding_space_key'])
        after = table_counts(conn)
        return {'status':'imported','run_id':run_id,'revision_hash':revision,
                'rows_added':{t:after[t]-before[t] for t in before}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=ROOT/'runs_real')
    p.add_argument('--run-key')
    p.add_argument('--skip-schema', action='store_true', help='Use existing tables without executing schema DDL')
    args = p.parse_args()
    try:
        with connect() as conn:
            if not args.skip_schema:
                apply_schema(conn)
            result = migrate(conn,args.source,args.run_key)
            print(json.dumps(result,indent=2,ensure_ascii=False))
    except (RuntimeError,ValueError) as exc:
        print(str(exc),file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
