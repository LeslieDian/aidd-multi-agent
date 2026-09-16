import json
import math
from pathlib import Path
from unittest.mock import patch
import pytest
from agents.evaluator import _composite_score, evaluate_candidates
from agents.working_memory import WorkingMemory
from tools.references import load_references
from tools.dock_score import validate_receptor, dock_smiles
from tools.provenance import digest, evaluation_protocol, file_hash
from loop import (
    evaluate_candidates_with_cache,
    filter_candidates_for_evaluation,
    load_config,
    run_loop,
)
from agents.failed_set import FailedLigandSet
from tools.evaluation_cache import EvaluationCache


def test_missing_score_is_not_rankable():
    val = {'valid': True, 'sa_score': 2., 'lipinski_pass': True}
    admet = {'valid': True, 'summary_score': .8}
    for score in (None, float('nan'), float('inf')):
        assert _composite_score(val, admet, {'valid': True, 'score': score}, {}) is None
    assert _composite_score(val, admet, {'valid': False, 'score': -9}, {}) is None
    good = _composite_score(val, admet, {'valid': True, 'score': -9}, {})
    bad = _composite_score({**val, 'lipinski_pass': False}, admet, {'valid': True, 'score': -9}, {})
    # Lipinski contributes 0.15 inside property, then property contributes
    # 0.55 to the multi-objective composite.
    assert good - bad == pytest.approx(.15 * .55)


def test_round_ids_survive_window():
    mem = WorkingMemory(3)
    for i in range(7):
        mem.add_round([], str(i))
    assert [r.round for r in mem.recent_rounds] == [4, 5, 6]
    assert 'R6: 6' in mem.compress_for_generator()


def test_reference_registry():
    refs = load_references()
    assert refs['afatinib']['MolecularFormula'] == 'C24H25ClFN5O3'
    assert '@' in refs['afatinib']['smiles']
    assert len(refs) == 3


def test_receptor_audit_detects_tampering(tmp_path):
    receptor = tmp_path / 'r.pdbqt'
    receptor.write_text('original')
    receptor.with_suffix('.audit.json').write_text(json.dumps({
        'preparation_passed': True, 'receptor_sha256': file_hash(receptor)}))
    validate_receptor(receptor)
    receptor.write_text('changed')
    with pytest.raises(ValueError, match='hash mismatch'):
        validate_receptor(receptor)


def test_docking_error_is_recorded(tmp_path):
    result = dock_smiles('CCO', tmp_path / 'missing.pdbqt', (0,0,0), artifact_dir=tmp_path)
    assert result['score'] is None and result['status'] == 'tool_error'
    assert (Path(result['artifacts']['directory']) / 'result.json').exists()


def test_previous_round_and_mock_isolation(tmp_path):
    cfg = load_config()
    production = Path('memory/failed_ligands.json')
    before = production.read_bytes()
    production_best = Path('memory/best_molecules.json')
    best_before = production_best.read_bytes() if production_best.exists() else None
    seen = []
    def judge(enriched, config, round_num, previous_focus, previous_summary,
              previous_enriched=None, use_mock=False):
        seen.append((previous_focus, previous_summary, previous_enriched))
        return {'focus': f'next-{round_num}', 'status': 'ok', 'is_mock': True}
    with patch('loop.judge_round', side_effect=judge):
        result = run_loop(cfg, str(tmp_path / 'run'), 3, 1, False, True, False)
    assert seen[0] == ('', None, None)
    assert seen[1][0] == 'next-0' and seen[1][1]['round'] == 0
    assert seen[2][1]['round'] == 1
    row = json.loads((tmp_path / 'run/round_1.json').read_text(encoding='utf-8'))
    assert row['focus_used'] == 'next-0' and row['focus_next'] == 'next-1'
    assert row['is_mock'] and row['protocol_id'] and row['run_id']
    assert all(c['composite_score'] is None for c in row['candidates'])
    assert all(c['protocol_id'] == row['protocol_id'] for c in row['candidates'])
    assert (tmp_path / 'run/_memory/best_molecules.json').exists() is False
    assert (tmp_path / 'run/_memory/strategy_history.json').exists()
    assert production.read_bytes() == before
    assert (production_best.read_bytes() if production_best.exists() else None) == best_before
    with pytest.raises(FileExistsError):
        run_loop(cfg, str(tmp_path / 'run'), 1, 1, False, True, False)


def test_config_controls_loop_defaults(tmp_path):
    cfg = load_config()
    cfg['loop']['max_rounds'] = 2
    cfg['loop']['candidates_per_round_per_generator'] = 1
    cfg['loop']['early_stop_patience'] = 9
    seen_n = []
    def generate(**kwargs):
        seen_n.append(kwargs['n_per_provider'])
        return [{
            'provider': 'mock', 'model': 'mock',
            'smiles_list': ['CCO'], 'rationale': 'test', 'usage': {},
        }]
    with patch('loop.generate_candidates', side_effect=generate):
        result = run_loop(
            cfg, str(tmp_path / 'configured'),
            dock_enabled=False, use_mock=True, verbose=False,
        )
    manifest = json.loads(
        (tmp_path / 'configured/manifest.json').read_text(encoding='utf-8')
    )
    assert result['rounds_completed'] == 2
    assert seen_n == [1, 1]
    assert manifest['execution']['max_rounds'] == 2
    assert manifest['execution']['candidates_per_round_per_generator'] == 1


def test_candidate_filter_removes_exact_repeats_but_keeps_invalid(tmp_path):
    failed = FailedLigandSet(path=tmp_path / 'failed.json')
    failed.add_failed('CCO', 'known failure')
    candidates = [
        {'smiles': 'OCC', 'provider': 'a'},       # canonical match to failed CCO
        {'smiles': 'CCN', 'provider': 'a'},
        {'smiles': 'NCC', 'provider': 'b'},       # canonical duplicate of CCN
        {'smiles': 'not-a-smiles', 'provider': 'b'},
    ]
    kept, stats = filter_candidates_for_evaluation(candidates, failed, 'off')
    assert [c['smiles'] for c in kept] == ['CCN', 'not-a-smiles']
    assert stats['known_failed_removed'] == 1
    assert stats['batch_duplicates_removed'] == 1
    assert stats['invalid_retained'] == 1
    assert kept[0]['duplicate_proposals'][0]['provider'] == 'b'


def test_evaluation_cache_reuses_result_and_preserves_current_identity(tmp_path):
    cfg = load_config()
    protocol_id = digest(evaluation_protocol(cfg['target'], cfg['scoring'], False))
    cache = EvaluationCache(tmp_path / 'cache', protocol_id)
    first_candidate = {
        'smiles': 'CCO', 'candidate_id': 'run-a:r0:c0', 'run_id': 'run-a',
        'round': 0, 'provider': 'deepseek', 'model': 'model-a',
    }
    second_candidate = {
        'smiles': 'OCC', 'candidate_id': 'run-b:r1:c2', 'run_id': 'run-b',
        'round': 1, 'provider': 'MiniMax', 'model': 'model-b',
    }
    with patch('loop.evaluate_candidates', wraps=evaluate_candidates) as evaluator:
        first, first_stats = evaluate_candidates_with_cache(
            [first_candidate], cfg['scoring'], cfg['target'], False,
            str(tmp_path / 'artifacts'), cache,
        )
        second, second_stats = evaluate_candidates_with_cache(
            [second_candidate], cfg['scoring'], cfg['target'], False,
            str(tmp_path / 'artifacts'), cache,
        )
    assert evaluator.call_count == 1
    assert first_stats == {
        'hits': 0, 'misses': 1, 'stored': 1, 'enabled': True,
        'docking_hits': 0, 'docking_misses': 0, 'docking_stored': 0,
    }
    assert second_stats == {
        'hits': 1, 'misses': 0, 'stored': 0, 'enabled': True,
        'docking_hits': 0, 'docking_misses': 0, 'docking_stored': 0,
    }
    assert second[0]['candidate_id'] == 'run-b:r1:c2'
    assert second[0]['provider'] == 'MiniMax'
    assert second[0]['evaluation_cache']['hit'] is True
    assert second[0]['evaluation_cache']['source']['candidate_id'] == 'run-a:r0:c0'
    assert second[0]['property_score'] == first[0]['property_score']


def test_complete_cache_requires_original_docking_artifact(tmp_path):
    artifact = tmp_path / 'source-artifact'
    artifact.mkdir()
    (artifact / 'result.json').write_text('{}', encoding='utf-8')
    cache = EvaluationCache(tmp_path / 'cache', 'protocol-1')
    evaluated = {
        'smiles': 'CCO', 'run_id': 'source-run', 'candidate_id': 'source-c0',
        'round': 0, 'provider': 'deepseek', 'model': 'm',
        'validate': {'valid': True}, 'admet': {'valid': True},
        'dock': {'valid': True, 'score': -7.0,
                 'artifacts': {'directory': str(artifact)}},
        'scaffold': None, 'composite_score': 0.7,
        'evaluation_status': 'complete', 'property_score': 0.7,
        'protocol_id': 'protocol-1', 'provenance': {'x': 1},
    }
    assert cache.put(evaluated)['stored'] is True
    assert cache.get('OCC') is not None
    (artifact / 'result.json').unlink()
    assert cache.get('CCO') is None


def test_evaluator_tool_failure_has_no_composite(tmp_path):
    cfg = load_config()
    with patch('agents.evaluator.dock_smiles', return_value={'score':None,'valid':False,'error':'timeout'}):
        row = evaluate_candidates([{'smiles':'CCO'}],cfg['scoring'],cfg['target'],artifact_dir=str(tmp_path))[0]
    assert row['evaluation_status'] == 'evaluation_error'
    assert row['composite_score'] is None
    assert row['property_score'] is not None


def test_judge_receives_previous_molecules_and_configured_provider():
    import agents.judge as module
    cfg = load_config()
    row = {'smiles':'CCO','validate':{'valid':True},'admet':{},'dock':{},'composite_score':None}
    client = type('Client', (), {'model':'test', 'chat':lambda self,*a,**k: json.dumps({'focus':'test', 'adopted_count':999})})()
    with patch.object(module, 'get_client', return_value=client) as factory:
        result = module.judge_round([row], cfg, 1, 'prior', {'top_candidates':[{'smiles':'CCN'}]}, use_mock=True)
    factory.assert_called_once_with(cfg['llm']['judge'], cfg, mock=True)
    assert 'CCN' in result['prompt']['user']
    assert result['adopted_count'] == 1


def test_invalid_llm_json_preserves_response():
    import agents.llm as module
    from agents.generator import generate_with_provider
    client = type('Client', (), {'model':'test', 'chat':lambda self,*a,**k: 'not json'})()
    with patch.object(module, 'get_client', return_value=client):
        result = generate_with_provider('deepseek', load_config(), use_mock=True)
    assert result['raw_response'] == 'not json'
    assert result['error'] and result['smiles_list'] == []
    assert result['prompt']['system']


def test_judge_error_never_invents_a_strategy():
    import agents.judge as module
    client = type('Client', (), {'model':'test', 'chat':lambda self,*a,**k: 'broken'})()
    with patch.object(module, 'get_client', return_value=client):
        result = module.judge_round([], load_config(), 0, use_mock=True)
    assert result['status'] == 'error' and result['focus'] == ''
    assert result['raw_response'] == 'broken'


def test_complete_candidates_precede_partial_candidates_in_judge():
    import agents.judge as module
    common = {'smiles':'CCO','validate':{'valid':True},'admet':{},'dock':{}}
    rows = [{**common, 'candidate_id':'partial','composite_score':None,'property_score':.99},
            {**common, 'candidate_id':'complete','composite_score':.2,'property_score':.4}]
    client = type('Client', (), {'model':'test', 'chat':lambda self,*a,**k: '{"focus":"test"}'})()
    with patch.object(module, 'get_client', return_value=client):
        result = module.judge_round(rows, load_config(), 0, use_mock=True)
    assert result['displayed_candidate_ids'] == ['complete','partial']


def test_protocol_changes_with_receptor_content(tmp_path):
    from tools.provenance import evaluation_protocol, digest
    path = tmp_path / 'r.pdbqt'
    path.write_text('old')
    old = digest(evaluation_protocol({'receptor_pdbqt':str(path)}, {}, False))
    path.write_text('new')
    assert digest(evaluation_protocol({'receptor_pdbqt':str(path)}, {}, False)) != old


def test_admet_failure_is_not_a_screening_success(tmp_path):
    cfg = load_config()
    with patch('agents.evaluator.estimate_admet', side_effect=RuntimeError('descriptor failed')):
        row = evaluate_candidates([{'smiles':'CCO'}],cfg['scoring'],cfg['target'],False)[0]
    assert row['evaluation_status'] == 'evaluation_error'
    assert row['composite_score'] is None and row['property_score'] is None
    assert row['admet']['error'] == 'RuntimeError: descriptor failed'


def test_invalid_smiles_preserves_input_and_protocol():
    cfg = load_config()
    row = evaluate_candidates([{'smiles':'not-a-molecule','candidate_id':'raw1'}],cfg['scoring'],cfg['target'],False)[0]
    assert row['evaluation_status'] == 'invalid_structure'
    assert row['smiles'] == 'not-a-molecule' and row['candidate_id'] == 'raw1'
    assert row['protocol_id']
