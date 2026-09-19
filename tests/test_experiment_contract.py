import copy
import json
from pathlib import Path
import pytest
import yaml

from experiments.contract import resolve_budget, validate_design, check_resume
from experiments.reporting import _confirmatory_decision, extract_run_metrics, build_report
from scripts.run_benchmark import assess_confirmatory_futility


def test_budget_matrix_wins_profile_but_explicit_smoke_stays_small():
    matrix = {'repeats': 20, 'rounds': 3}
    profile = {'repeats': 10, 'rounds': 1}
    assert resolve_budget({}, matrix, profile)['repeats'] == 20
    assert resolve_budget({'repeats': 2}, matrix, profile)['repeats'] == 2
    assert resolve_budget({}, matrix, profile, smoke=True)['rounds'] == 1
    with pytest.raises(ValueError):
        resolve_budget({'repeats': 0}, matrix, profile)


def test_multi_design_rejects_unsupported_or_ambiguous_rules():
    matrix = yaml.safe_load(Path('experiments/confirmatory_matrix_v4.yaml').read_text(encoding='utf-8'))
    validate_design(matrix, matrix['groups'])
    matrix['confirmatory']['treatment_group'] = 'reflection_memory'
    with pytest.raises(ValueError, match='not both'):
        validate_design(matrix, matrix['groups'])
    del matrix['confirmatory']['treatment_group']
    matrix['confirmatory']['futility']['interim_looks'] = 2
    with pytest.raises(ValueError, match='Unsupported'):
        validate_design(matrix, matrix['groups'])


def test_multi_futility_does_not_stop_a_still_reachable_arm(tmp_path):
    manifest = {'execution': {'repeats': 4}, 'confirmatory': {
        'treatment_groups': ['a', 'b'], 'min_improved_run_rate': .75,
        'futility': {'enabled': True, 'min_completed': 2}}, 'runs': []}
    for arm, outcomes in [('a', [False, False]), ('b', [True, True])]:
        for i, improved in enumerate(outcomes):
            d = tmp_path / f'{arm}{i}'; d.mkdir()
            (d / 'summary.json').write_text(json.dumps({'agent_metrics': {'run_shows_improvement': improved}}))
            manifest['runs'].append({'group': arm, 'repeat': i, 'eligible': True, 'run_dir': str(d)})
    result = assess_confirmatory_futility(manifest)
    assert result['treatments']['a']['stop']
    assert result['treatments']['a']['minimum_successes'] == 3
    assert not result['stop']
    for row in manifest['runs']:
        (Path(row['run_dir']) / 'summary.json').write_text(json.dumps({'agent_metrics': {'run_shows_improvement': False}}))
    assert assess_confirmatory_futility(manifest)['stop']


def test_multi_confirmation_corrects_alpha_and_keeps_both_decisions():
    manifest = {'baseline_group': 'r', 'confirmatory': {'reference_group': 'r',
        'treatment_groups': ['a', 'b'], 'alpha': .05, 'multiplicity': 'bonferroni'}}
    metrics = {'run_improvement_rate': {'mean': .8}, 'best_vina_delta': {'mean': -.2},
               'mean_herg_risk_score': {'values': [.1, .1, .1]}}
    groups = {x: {'metrics': copy.deepcopy(metrics)} for x in ['r', 'a', 'b']}
    comparisons = {x: {'best_safe_composite_global': {'favorable': True, 'welch_p_value': p,
                    'statistically_significant': True}} for x, p in [('a', .04), ('b', .01)]}
    result = _confirmatory_decision(manifest, groups, comparisons, 'best_safe_composite_global')
    assert set(result['treatments']) == {'a', 'b'}
    assert result['per_treatment_alpha'] == .025
    assert not result['treatments']['a']['efficacy_supported']
    assert result['treatments']['b']['efficacy_supported']
    manifest['execution'] = {'repeats': 20}
    result = _confirmatory_decision(manifest, groups, comparisons, 'best_safe_composite_global')
    assert result['treatments']['b']['status'] == 'insufficient_repeats'


def test_resume_rejects_config_and_source_drift(tmp_path):
    import hashlib
    source = tmp_path / 'loop.py'; source.write_text('original')
    run = tmp_path / 'attempt'; run.mkdir()
    config = {'loop': {'memory_namespace': 'old'}, 'llm': {'generators': ['MiniMax']}}
    (run / 'resolved_config.yaml').write_text(yaml.safe_dump(config))
    (run / 'manifest.json').write_text(json.dumps({'code_hashes': {'loop.py': hashlib.sha256(source.read_bytes()).hexdigest()}}))
    manifest = {'groups': {'a': {'overrides': {}}}, 'execution': {}, 'runs': [{'group': 'a', 'run_dir': str(run)}]}
    matrix = {'groups': manifest['groups']}
    assert check_resume(tmp_path, manifest, matrix, {'a': config})['checked_attempts'] == 1
    changed = copy.deepcopy(config); changed['llm']['generators'] = ['different']
    with pytest.raises(ValueError, match='config changed'):
        check_resume(tmp_path, manifest, matrix, {'a': changed})
    source.write_text('changed')
    with pytest.raises(ValueError, match='source changed'):
        check_resume(tmp_path, manifest, matrix, {'a': config})


def test_safe_metrics_do_not_count_unsafe_gain_or_missing_endpoint(tmp_path):
    (tmp_path / 'summary.json').write_text(json.dumps({'agent_metrics': {'best_vina_delta': -1, 'run_shows_improvement': True}}))
    def candidate(score, safe):
        return {'smiles': str(score), 'validate': {'valid': True}, 'evaluation_status': 'complete',
                'safety_gate_pass': safe, 'dock': {'score': score}}
    for n, scores in enumerate([[-7, -8], [-6, -9]]):
        (tmp_path / f'round_{n}.json').write_text(json.dumps({'candidates': [candidate(scores[0], True), candidate(scores[1], False)]}))
    result = extract_run_metrics(tmp_path)
    assert result['best_vina_delta'] == -1
    assert result['best_safe_vina_delta'] == 1
    assert result['safe_run_improvement_rate'] == 0
    (tmp_path / 'round_1.json').write_text(json.dumps({'candidates': [candidate(-9, False)]}))
    assert extract_run_metrics(tmp_path)['safe_run_improvement_rate'] is None


def test_four_arm_report_has_component_comparisons_and_failed_cost(tmp_path):
    groups = {x: {} for x in ['reflection', 'reflection_failed_set', 'reflection_memory']}
    manifest = {'groups': groups, 'benchmark_id': 'test', 'execution': {}, 'baseline_group': 'reflection'}
    (tmp_path / 'benchmark_manifest.json').write_text(json.dumps(manifest))
    for arm in groups:
        for n, eligible in [(1, False), (2, True)]:
            d = tmp_path / arm / 'repeat_01' / f'attempt_{n:02d}'; d.mkdir(parents=True)
            (d / 'summary.json').write_text(json.dumps({'status': 'finished' if eligible else 'error', 'tokens_used': 100}))
            (d / 'benchmark_run.json').write_text(json.dumps({'group': arm, 'repeat': 1, 'eligible': eligible, 'duration_seconds': 2}))
    r = build_report(tmp_path)
    assert 'reflection_failed_set_vs_reflection' in r['incremental_comparisons']
    assert 'reflection_memory_vs_reflection_failed_set' in r['incremental_comparisons']
    assert r['attempt_accounting']['reflection']['recorded_tokens_all_attempts'] == 200
    assert r['groups']['reflection']['runs'] == 1
