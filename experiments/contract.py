"""Shared experiment configuration contract; no model or evaluation calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path


def treatments(spec):
    if 'treatment_group' in spec and 'treatment_groups' in spec:
        raise ValueError('Use treatment_group OR treatment_groups, not both')
    result = spec.get('treatment_groups', [spec.get('treatment_group', 'reflection_memory')])
    if not isinstance(result, list) or not result or any(not isinstance(x, str) for x in result):
        raise ValueError('treatment_groups must be a nonempty list of names')
    if len(set(result)) != len(result):
        raise ValueError('Duplicate treatment groups')
    return result


def validate_design(matrix, selected):
    spec = matrix.get('confirmatory') or {}
    if not spec:
        return
    allowed = {'reference_group', 'treatment_group', 'treatment_groups', 'safety_metric',
               'safety_noninferiority_margin', 'min_improved_run_rate', 'alpha', 'futility',
               'improvement_metric', 'improvement_delta_metric', 'multiplicity'}
    if set(spec) - allowed:
        raise ValueError(f'Unknown confirmatory fields: {sorted(set(spec) - allowed)}')
    arms = treatments(spec)
    reference = spec.get('reference_group', 'reflection')
    if reference in arms or not set([reference, *arms]) <= set(selected):
        raise ValueError('Missing or overlapping confirmatory groups')
    if not 0 < float(spec.get('alpha', .05)) < 1 or not 0 <= float(spec.get('min_improved_run_rate', .7)) <= 1:
        raise ValueError('Invalid confirmatory alpha or improvement rate')
    if len(arms) > 1 and spec.get('multiplicity') != 'bonferroni':
        raise ValueError('Multiple treatments require multiplicity: bonferroni')
    if spec.get('improvement_metric', 'run_improvement_rate') not in {'run_improvement_rate', 'safe_run_improvement_rate'}:
        raise ValueError('Unknown improvement metric')
    if spec.get('improvement_delta_metric', 'best_vina_delta') not in {'best_vina_delta', 'best_safe_vina_delta'}:
        raise ValueError('Unknown improvement delta metric')
    futility = spec.get('futility') or {}
    if set(futility) - {'enabled', 'min_completed', 'rule'}:
        raise ValueError('Unsupported futility fields')
    if futility.get('enabled') and futility.get('rule') != 'stop_when_minimum_improvement_rate_is_mathematically_unreachable':
        raise ValueError('Unsupported futility rule')


def resolve_budget(cli, execution, profile, smoke=False):
    """CLI > matrix > profile defaults; smoke retains its explicitly small preset."""
    result = {}
    for key, default in [('repeats', 3), ('rounds', 3), ('candidates_per_round_per_generator', 5)]:
        sources = [cli, profile, execution] if smoke else [cli, execution, profile]
        value = next((s[key] for s in sources if s.get(key) is not None), default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f'{key} must be a positive integer')
        result[key] = value
    return result


def normalized_config(config):
    result = deepcopy(config)
    result.setdefault('loop', {}).update(require_all_generators=True, generation_max_attempts=3, judge_max_attempts=3)
    result['loop'].pop('memory_namespace', None)
    return result


def contract_hashes(root):
    return {name: hashlib.sha256((Path(root) / name).read_bytes()).hexdigest() for name in
            ['scripts/run_benchmark.py', 'experiments/contract.py', 'experiments/reporting.py']}


def check_resume(root, manifest, matrix, configs):
    """Fail before writes/API calls; old attempts are the frozen execution evidence."""
    if manifest.get('contract_hashes') and manifest['contract_hashes'] != contract_hashes(root):
        raise ValueError('resume experiment contract source changed')
    if manifest.get('resolved_group_configs') and manifest['resolved_group_configs'] != {
            group: normalized_config(config) for group, config in configs.items()}:
        raise ValueError('resume frozen group config changed')
    for group, old in manifest['groups'].items():
        if old.get('overrides', {}) != matrix['groups'][group].get('overrides', {}):
            raise ValueError(f'resume group overrides changed: {group}')
    if manifest.get('confirmatory') != matrix.get('confirmatory'):
        raise ValueError('resume confirmatory rules changed')
    for key, default in [('primary_metric', 'best_composite_global'), ('order', 'grouped')]:
        if manifest['execution'].get(key, default) != matrix.get('execution', {}).get(key, default):
            raise ValueError(f'resume {key} changed')
    checked = 0
    for run in manifest.get('runs', []):
        folder = Path(run['run_dir'])
        saved = folder / 'resolved_config.yaml'
        provenance = folder / 'manifest.json'
        if not saved.exists() or not provenance.exists():
            raise ValueError(f'Missing resume provenance: {folder}')
        import yaml
        old = yaml.safe_load(saved.read_text(encoding='utf-8'))
        if normalized_config(old) != normalized_config(configs[run['group']]):
            raise ValueError(f'resume resolved config changed: {run["group"]}')
        record = json.loads(provenance.read_text(encoding='utf-8'))
        for name, expected in record.get('code_hashes', {}).items():
            path = Path(root) / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f'resume execution source changed: {name}')
        for field, name in [('reference_registry_sha256', 'data/reference_compounds.json'),
                            ('sa_fragment_model_sha256', 'tools/fpscores.pkl.gz')]:
            if record.get(field) and hashlib.sha256((Path(root) / name).read_bytes()).hexdigest() != record[field]:
                raise ValueError(f'resume scoring asset changed: {name}')
        checked += 1
    return {'checked_attempts': checked, 'config_and_recorded_source_match': True}
