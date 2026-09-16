"""agents/evaluator.py - Agent B (no LLM, calls Phase 1 tools).

For each candidate SMILES, runs all 4 scoring tools and returns a
merged record. Failures are isolated per molecule.
"""
from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tools.provenance import digest, evaluation_protocol
from tools.validate_mol import validate_smiles, lipinski_pass
from tools.admet_score import estimate_admet

from tools import (
    dock_smiles,
    get_scaffold,
)


def evaluate_candidates(
    candidates: list[dict],
    scoring_config: dict,
    target_config: dict,
    dock_enabled: bool = True,
    artifact_dir: str | None = None,
    dock_overrides: dict[str, dict] | None = None,
) -> list[dict]:
    """Evaluate a batch of candidates.

    Args:
        candidates: list of {"smiles": str, "provider": str, ...}
        scoring_config: config["scoring"] dict
        target_config: config["target"] dict
        dock_enabled: skip Vina if False (faster iteration in dev)

    Returns:
        list of enriched candidates with keys:
            smiles, provider, model, rationale,
            validate, admet, dock, scaffold,
            composite_score
    """
    smiles_list = [c["smiles"] for c in candidates]

    # ---- 1) validate_mol + admet (cheap, always run) ----
    val_results = [_safe_call(validate_smiles, smi) for smi in smiles_list]
    for value in val_results:
        if value.get("valid"):
            value["lipinski_pass"] = lipinski_pass(value, **scoring_config.get("lipinski", {}))
    admet_results = [_safe_call(estimate_admet, smi) for smi in smiles_list]

    # ---- 2) dock (slow; parallel and optionally use a two-stage funnel) ----
    pocket = target_config["pocket"]
    receptor = target_config["receptor_pdbqt"]
    dock_results: list[dict | None] = [None] * len(smiles_list)
    screening_results: list[dict | None] = [None] * len(smiles_list)
    dock_overrides = dock_overrides or {}
    if dock_enabled:
        full_cfg = dict(scoring_config.get("vina", {}) or {})
        funnel_cfg = dict(scoring_config.get("funnel", {}) or {})
        funnel_enabled = bool(funnel_cfg.get("enabled", False))
        valid_indices = [i for i, value in enumerate(val_results) if value.get("valid")]

        selected_indices = set(valid_indices)
        if funnel_enabled and valid_indices:
            fast_cfg = dict(full_cfg)
            fast_cfg.update(funnel_cfg.get("fast_vina", {}) or {})
            fast_jobs = [
                (index, smiles_list[index]) for index in valid_indices
                if smiles_list[index] not in dock_overrides
            ]
            screening_results = _run_docking_jobs(
                fast_jobs, len(smiles_list), receptor, pocket, fast_cfg, artifact_dir
            )
            for index in valid_indices:
                smi = smiles_list[index]
                if smi in dock_overrides:
                    screening_results[index] = {
                        "smiles": smi,
                        "score": dock_overrides[smi].get("score"),
                        "valid": bool(dock_overrides[smi].get("valid")),
                        "status": "full_cache_hit",
                        "error": None,
                    }

            fraction = max(0.0, min(1.0, float(funnel_cfg.get("full_dock_fraction", 0.35))))
            minimum = max(1, int(funnel_cfg.get("min_full_dock", 3)))
            requested = max(minimum, math.ceil(len(valid_indices) * fraction))
            maximum = int(funnel_cfg.get("max_full_dock", 0) or 0)
            if maximum > 0:
                requested = min(requested, maximum)
            requested = min(requested, len(valid_indices))

            ranked = sorted(
                valid_indices,
                key=lambda index: _funnel_priority(
                    val_results[index], admet_results[index],
                    screening_results[index] or {}, scoring_config,
                ),
                reverse=True,
            )
            selected_indices = set(ranked[:requested])
            # A protocol-compatible full docking cache hit is always retained;
            # re-running a cheaper screen would waste work and can only discard evidence.
            selected_indices.update(
                index for index in valid_indices if smiles_list[index] in dock_overrides
            )

        full_jobs = []
        for index, (smi, validation) in enumerate(zip(smiles_list, val_results)):
            if not validation.get("valid"):
                dock_results[index] = {
                    "smiles": smi, "score": None, "valid": False,
                    "status": "invalid_structure", "error": "skipped (invalid SMILES)",
                }
            elif index not in selected_indices:
                dock_results[index] = {
                    "smiles": smi, "score": None, "valid": False,
                    "status": "screened_out",
                    "error": "not selected by the configured two-stage funnel",
                }
            elif smi in dock_overrides:
                dock_results[index] = dock_overrides[smi]
            else:
                full_jobs.append((index, smi))
        computed = _run_docking_jobs(
            full_jobs, len(smiles_list), receptor, pocket, full_cfg, artifact_dir
        )
        for index, _ in full_jobs:
            dock_results[index] = computed[index]
    else:
        for index, smi in enumerate(smiles_list):
            dock_results[index] = {
                "smiles": smi, "score": None, "valid": False, "status": "disabled",
                "error": "dock disabled",
            }

    # ---- 3) scaffolds ----
    scaffolds = [get_scaffold(s) for s in smiles_list]

    # ---- 4) Merge and compute composite ----
    protocol = evaluation_protocol(target_config, scoring_config, dock_enabled)
    enriched = []
    for index, (cand, val, admet, dock, scaf) in enumerate(zip(
        candidates, val_results, admet_results, dock_results, scaffolds
    )):
        dock = dock or {
            "smiles": cand.get("smiles"), "score": None, "valid": False,
            "status": "tool_error", "error": "missing docking result",
        }
        screen = screening_results[index]
        funnel = {
            "enabled": bool(dock_enabled and (scoring_config.get("funnel", {}) or {}).get("enabled")),
            "selected_for_full_dock": dock.get("status") != "screened_out",
            "screening_dock": screen,
        }
        components = _score_components(val, admet, dock, scoring_config)
        composite = _composite_score(val, admet, dock, scoring_config)
        prop = _property_score(val, admet, scoring_config)
        status = ("evaluation_error" if val.get("status") == "tool_error" else "invalid_structure" if not val.get("valid") else
                  "complete" if composite is not None else
                  "screened_out" if dock.get("status") == "screened_out" else
                  "screening_only" if not dock_enabled and prop is not None else "evaluation_error")
        enriched.append({
            **cand,
            "validate": val,
            "admet": admet,
            "dock": dock,
            "scaffold": scaf,
            "composite_score": composite,
            "evaluation_status": status,
            "property_score": prop,
            "score_components": components,
            "safety_gate_pass": _safety_gate_pass(val, admet, scoring_config),
            "funnel": funnel,
            "protocol_id": digest(protocol),
            "provenance": protocol,
        })
    assign_pareto_metadata(enriched, scoring_config)
    return enriched


def _resolve_docking_workers(vina_cfg: dict, job_count: int) -> int:
    """Resolve bounded process concurrency for the CPU-only Vina binary."""
    if job_count <= 1:
        return max(1, job_count)
    requested = vina_cfg.get("workers", 1)
    cpu_per_job = max(1, int(vina_cfg.get("cpu", 1)))
    if str(requested).lower() == "auto":
        requested = max(1, (os.cpu_count() or 1) // cpu_per_job)
    return max(1, min(int(requested), job_count))


def _run_docking_jobs(jobs, total, receptor, pocket, vina_cfg, artifact_dir):
    """Run independent Vina subprocesses concurrently and preserve input order."""
    results: list[dict | None] = [None] * total
    if not jobs:
        return results

    kwargs = {
        "exhaustiveness": int(vina_cfg.get("exhaustiveness", 8)),
        "n_poses": int(vina_cfg.get("n_poses", 5)),
        "seed": int(vina_cfg.get("seed", 2026)),
        "cpu": int(vina_cfg.get("cpu", 2)),
        "timeout": int(vina_cfg.get("timeout", 180)),
        "artifact_dir": artifact_dir,
    }
    center = (pocket["center_x"], pocket["center_y"], pocket["center_z"])
    size = (pocket["size_x"], pocket["size_y"], pocket["size_z"])

    def run_one(index, smiles):
        try:
            return index, dock_smiles(smiles, receptor, center, size, **kwargs)
        except Exception as exc:
            return index, {
                "smiles": smiles, "score": None, "valid": False,
                "status": "tool_error", "error": f"{type(exc).__name__}: {exc}",
            }

    workers = _resolve_docking_workers(vina_cfg, len(jobs))
    if workers == 1:
        for index, smiles in jobs:
            result_index, result = run_one(index, smiles)
            results[result_index] = result
        return results

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vina") as executor:
        futures = [executor.submit(run_one, index, smiles) for index, smiles in jobs]
        for future in as_completed(futures):
            index, result = future.result()
            results[index] = result
    return results


def _funnel_priority(val, admet, screening_dock, cfg):
    """Safety-first rank for promotion from fast screen to full docking."""
    prop = _property_score(val, admet, cfg)
    score = screening_dock.get("score")
    risk = admet.get("herg_risk_score", admet.get("herg_risk", 1.0))
    return (
        bool(_safety_gate_pass(val, admet, cfg)),
        prop if finite(prop) else -math.inf,
        -float(score) if screening_dock.get("valid") and finite(score) else -math.inf,
        -float(risk) if finite(risk) else -math.inf,
    )


def _safe_call(function, smiles):
    try:
        return function(smiles)
    except Exception as exc:
        return {'smiles': smiles, 'valid': False, 'status': 'tool_error',
                'error': f'{type(exc).__name__}: {exc}'}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _property_score(val, admet, cfg):
    if val.get('status') == 'tool_error':
        return None
    if not val.get('valid'):
        return 0.0
    sa = val.get('sa_score')
    quality = admet.get('admet_quality_score', admet.get('summary_score'))
    if not admet.get('valid') or not finite(quality) or not finite(sa):
        return None
    sa_component = max(0.0, min(1.0, 1.0 - (sa - 1.0) / 9.0))
    if sa > cfg.get('sa_score', {}).get('reject_above', 6.0):
        sa_component *= .3
    return .55 * quality + .15 * bool(val.get('lipinski_pass')) + .30 * sa_component


def _score_components(val, admet, dock, cfg):
    prop = _property_score(val, admet, cfg)
    vina = dock.get('score')
    risk = admet.get('herg_risk_score', admet.get('herg_risk', 0.0))
    if prop is None or not dock.get('valid') or not finite(vina) or not finite(risk):
        return None
    objective = cfg.get('objective', {}) or {}
    vina_component = max(0.0, min(1.0, -vina / float(objective.get('vina_scale', 12.0))))
    if vina > cfg.get('vina', {}).get('accept_below', -7.0):
        vina_component *= float(objective.get('weak_vina_multiplier', 0.3))
    weights = objective.get('weights', {}) or {}
    normalized = {
        'property': float(weights.get('property', 0.55)),
        'vina': float(weights.get('vina', 0.30)),
        'herg_safety': float(weights.get('herg_safety', 0.15)),
    }
    total = sum(normalized.values())
    if total <= 0:
        raise ValueError('scoring.objective.weights must sum to a positive value')
    normalized = {key: value / total for key, value in normalized.items()}
    return {
        'property': prop,
        'vina': vina_component,
        'herg_safety': 1.0 - risk,
        'weights': normalized,
    }


def _composite_score(val, admet, dock, cfg):
    """Configurable high-precision utility; missing evidence remains unknown."""
    if val.get('status') == 'tool_error':
        return None
    if not val.get('valid'):
        return 0.0
    components = _score_components(val, admet, dock, cfg)
    if components is None:
        return None
    weights = components['weights']
    value = sum(components[name] * weights[name] for name in weights)
    precision = int((cfg.get('objective', {}) or {}).get('precision', 6))
    return round(value, precision)


def _safety_gate_pass(val, admet, cfg):
    if not val.get('valid') or not admet.get('valid'):
        return False
    safety = ((cfg.get('pareto', {}) or {}).get('safety', {}) or {})
    risk = admet.get('herg_risk_score', admet.get('herg_risk', 0.0))
    logp = admet.get('logp', val.get('logp'))
    if not finite(risk) or not finite(logp):
        return False
    return (
        risk <= float(safety.get('max_herg_risk_score', 0.5))
        and logp <= float(safety.get('max_logp', 4.5))
    )


def _dominates(left: tuple[float, ...], right: tuple[float, ...]) -> bool:
    return all(a <= b for a, b in zip(left, right)) and any(
        a < b for a, b in zip(left, right)
    )


def assign_pareto_metadata(enriched: list[dict], cfg: dict) -> list[dict]:
    """Assign non-dominated ranks using Vina, properties, hERG risk, and SA."""
    eligible: list[tuple[int, tuple[float, ...]]] = []
    for index, candidate in enumerate(enriched):
        admet = candidate.get('admet') or {}
        validate = candidate.get('validate') or {}
        dock = candidate.get('dock') or {}
        values = (
            dock.get('score'),
            candidate.get('property_score'),
            admet.get('herg_risk_score', admet.get('herg_risk', 0.0)),
            validate.get('sa_score'),
        )
        if candidate.get('evaluation_status') != 'complete' or not all(finite(v) for v in values):
            candidate.update(pareto_rank=None, pareto_dominated_by=None)
            continue
        vector = (float(values[0]), 1.0 - float(values[1]), float(values[2]), float(values[3]))
        candidate['pareto_objectives'] = {
            'vina': vector[0],
            'property_loss': vector[1],
            'herg_risk_score': vector[2],
            'sa_score': vector[3],
        }
        eligible.append((index, vector))

    remaining = list(eligible)
    rank = 1
    while remaining:
        front = [
            item for item in remaining
            if not any(_dominates(other[1], item[1]) for other in remaining if other[0] != item[0])
        ]
        for index, vector in front:
            dominated_by = sum(_dominates(other, vector) for _, other in eligible)
            enriched[index]['pareto_rank'] = rank
            enriched[index]['pareto_dominated_by'] = dominated_by
        front_indices = {item[0] for item in front}
        remaining = [item for item in remaining if item[0] not in front_indices]
        rank += 1
    return enriched


def candidate_priority_key(candidate: dict) -> tuple:
    """Sort safe Pareto candidates first, then use precise utility."""
    rank = candidate.get('pareto_rank')
    composite = candidate.get('composite_score')
    return (
        bool(candidate.get('safety_gate_pass')),
        -(rank if isinstance(rank, int) else 10**6),
        composite if finite(composite) else -math.inf,
    )


def summarize_round(enriched):
    valid = [c for c in enriched if c['validate'].get('valid')]
    docked = [c for c in valid if c['dock'].get('valid') and finite(c['dock'].get('score'))]
    complete = [c for c in valid if finite(c.get('composite_score'))]
    properties = [c['admet']['summary_score'] for c in valid
                  if c['admet'].get('valid') and finite(c['admet'].get('summary_score'))]
    best = min(docked, key=lambda c: c['dock']['score']) if docked else None
    ranked = sorted(complete, key=candidate_priority_key, reverse=True)[:3]
    pareto_front = [c for c in complete if c.get('pareto_rank') == 1]
    safe = [c for c in complete if c.get('safety_gate_pass')]
    funnel_enabled = any((c.get('funnel') or {}).get('enabled') for c in enriched)
    screened_out = [c for c in enriched if c.get('evaluation_status') == 'screened_out']
    fast_docked = [
        c for c in enriched
        if (((c.get('funnel') or {}).get('screening_dock') or {}).get('valid'))
    ]
    return {
        'n_total': len(enriched), 'n_valid': len(valid), 'n_complete': len(complete),
        'valid_ratio': round(len(valid) / max(len(enriched), 1), 3),
        'n_docked': len(docked),
        'n_safety_pass': len(safe),
        'safety_pass_rate': round(len(safe) / max(len(complete), 1), 6),
        'pareto_front_size': len(pareto_front),
        'safe_pareto_front_size': sum(c.get('safety_gate_pass') for c in pareto_front),
        'funnel_enabled': funnel_enabled,
        'n_fast_docked': len(fast_docked),
        'n_screened_out': len(screened_out),
        'n_promoted_to_full_dock': len(docked) if funnel_enabled else None,
        'n_unique_scaffolds': len({c['scaffold'] for c in valid if c.get('scaffold')}),
        'avg_admet': round(sum(properties) / len(properties), 3) if properties else None,
        'best_vina': best['dock']['score'] if best else None,
        'best_smiles': best['smiles'] if best else None,
        'top_candidates': [{'candidate_id': c.get('candidate_id'), 'smiles': c['smiles'],
                            'score': c['composite_score'], 'vina': c['dock']['score'],
                            'pareto_rank': c.get('pareto_rank'),
                            'safety_gate_pass': c.get('safety_gate_pass')}
                           for c in ranked],
    }
