"""loop.py - Main iterative loop for AIDD Multi-Agent (Phase 4.1 enabled).

Usage:
    # Real mode (requires .env with API keys)
    python loop.py

    # Mock mode (no API keys needed, uses pre-set molecules)
    python loop.py --mock

    # Custom target / rounds
    python loop.py --rounds 5 --output runs/ --mock

    # Phase 4.1: enable HITL approval + Memory + FailedSet
    python loop.py --rounds 5 --hitl

Output: runs/round_0.json ... runs/round_{n-1}.json + summary.json
"""
from __future__ import annotations

import argparse
import copy
import re
import uuid
from tools.provenance import digest, file_hash, evaluation_protocol, docking_protocol
from tools.dock_score import validate_receptor
from tools.evaluation_cache import EvaluationCache
from tools.docking_cache import DockingCache
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from agents.generator import generate_candidates
from agents.evaluator import evaluate_candidates, summarize_round, assign_pareto_metadata
from agents.judge import judge_round
# Phase 4.1
from agents.loop_controller import LoopController, LoopState, LoopConfig
from agents.failed_set import FailedLigandSet
from agents.working_memory import WorkingMemory
from agents.hitl import HITLCheckpoint
# Phase 4.3 (P2-3): agent-level metrics aggregation
from agents.agent_metrics import compute_agent_metrics


def load_config(path: str = "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def flatten_generator_results(gen_results: list[dict]) -> list[dict]:
    """Turn one or more generator outputs into a flat list of candidates."""
    flat = []
    for r in gen_results:
        provider = r.get("provider", "unknown")
        model = r.get("model", "unknown")
        rationale = r.get("rationale", "")
        if r.get("error"):
            print(f"  [!] provider {provider} failed: {r['error']}")
            continue
        for smi in r.get("smiles_list", []):
            flat.append({
                "smiles": smi,
                "provider": provider,
                "model": model,
                "rationale": rationale,
            })
    return flat


def filter_candidates_for_evaluation(
    candidates: list[dict],
    failed_set: FailedLigandSet,
    similarity_mode: str = "report",
) -> tuple[list[dict], dict]:
    """Remove exact repeats before expensive tools and report fuzzy matches.

    Invalid SMILES are deliberately retained so the evaluator can record an
    ``invalid_structure`` result.  Embedding similarity is observational by
    default because the configured sentence model is not a validated chemical
    similarity model.
    """
    if similarity_mode not in {"off", "report", "exclude"}:
        raise ValueError("failed_set.embeddings.mode must be off, report, or exclude")

    kept: list[dict] = []
    seen: dict[str, int] = {}
    stats = {
        "raw": len(candidates),
        "kept": 0,
        "batch_duplicates_removed": 0,
        "known_failed_removed": 0,
        "similar_to_failed_flagged": 0,
        "similar_to_failed_removed": 0,
        "invalid_retained": 0,
        "similarity_mode": similarity_mode,
    }
    for original in candidates:
        candidate = dict(original)
        smiles = candidate.get("smiles", "")
        canonical = failed_set.canonicalize(smiles)
        if canonical is None:
            stats["invalid_retained"] += 1
            kept.append(candidate)
            continue
        if canonical in seen:
            stats["batch_duplicates_removed"] += 1
            kept[seen[canonical]].setdefault("duplicate_proposals", []).append({
                "smiles": smiles,
                "provider": candidate.get("provider"),
                "model": candidate.get("model"),
            })
            continue
        if failed_set.is_failed(canonical):
            stats["known_failed_removed"] += 1
            continue

        if similarity_mode != "off" and failed_set.enable_embeddings:
            too_close, matched, similarity = failed_set.is_similar_to_failed(canonical)
            if too_close:
                stats["similar_to_failed_flagged"] += 1
                candidate["failed_similarity"] = {
                    "matched_smiles": matched,
                    "similarity": round(similarity, 6),
                    "action": similarity_mode,
                }
                if similarity_mode == "exclude":
                    stats["similar_to_failed_removed"] += 1
                    continue
        seen[canonical] = len(kept)
        kept.append(candidate)

    stats["kept"] = len(kept)
    return kept, stats


def evaluate_candidates_with_cache(
    candidates: list[dict],
    scoring_config: dict,
    target_config: dict,
    dock_enabled: bool,
    artifact_dir: str,
    cache: EvaluationCache,
    docking_cache: DockingCache | None = None,
) -> tuple[list[dict], dict]:
    """Evaluate cache misses and restore results in proposal order."""
    results: list[dict | None] = [None] * len(candidates)
    misses: list[dict] = []
    miss_indices: list[int] = []
    stats = {
        "hits": 0, "misses": 0, "stored": 0, "enabled": cache.enabled,
        "docking_hits": 0, "docking_misses": 0, "docking_stored": 0,
    }

    for index, candidate in enumerate(candidates):
        payload = cache.get(candidate.get("smiles", ""))
        if payload is None:
            misses.append(candidate)
            miss_indices.append(index)
            stats["misses"] += 1
        else:
            results[index] = cache.materialize(candidate, payload)
            stats["hits"] += 1

    if misses:
        dock_overrides = {}
        docking_payloads = {}
        if dock_enabled and docking_cache is not None:
            for candidate in misses:
                smiles = candidate.get("smiles", "")
                payload = docking_cache.get(smiles)
                if payload is None:
                    stats["docking_misses"] += 1
                else:
                    dock_overrides[smiles] = docking_cache.materialize(payload)
                    docking_payloads[smiles] = payload
                    stats["docking_hits"] += 1
        evaluated_misses = evaluate_candidates(
            candidates=misses,
            scoring_config=scoring_config,
            target_config=target_config,
            dock_enabled=dock_enabled,
            artifact_dir=artifact_dir,
            dock_overrides=dock_overrides,
        )
        for index, evaluated in zip(miss_indices, evaluated_misses):
            smiles = evaluated.get("smiles", "")
            docking_payload = docking_payloads.get(smiles)
            if docking_payload is not None:
                evaluated["docking_cache"] = {
                    "hit": True,
                    "docking_protocol_id": docking_cache.protocol_id,
                    "cache_entry": docking_payload.get("cache_entry"),
                    "source": docking_payload.get("source") or {},
                }
            elif dock_enabled and docking_cache is not None:
                docking_stored = docking_cache.put(
                    smiles,
                    evaluated.get("dock") or {},
                    source={
                        "run_id": evaluated.get("run_id"),
                        "candidate_id": evaluated.get("candidate_id"),
                        "round": evaluated.get("round"),
                    },
                )
                evaluated["docking_cache"] = {
                    "hit": False,
                    "stored": bool(docking_stored.get("stored")),
                    "docking_protocol_id": docking_cache.protocol_id,
                    "cache_entry": docking_stored.get("cache_entry"),
                    "source": docking_stored.get("source") or {},
                }
                if docking_stored.get("stored"):
                    stats["docking_stored"] += 1
            stored = cache.put(evaluated)
            evaluated["evaluation_cache"] = {
                "hit": False,
                "stored": bool(stored.get("stored")),
                "reason": stored.get("reason"),
                "protocol_id": evaluated.get("protocol_id"),
                "canonical_smiles": stored.get("canonical_smiles"),
                "cache_key": stored.get("cache_key"),
                "cache_entry": stored.get("cache_entry"),
                "source": stored.get("source"),
            }
            if stored.get("stored"):
                stats["stored"] += 1
            results[index] = evaluated

    ordered = [result for result in results if result is not None]
    assign_pareto_metadata(ordered, scoring_config)
    return ordered, stats


def run_loop(
    config: dict,
    output_dir: str = "runs",
    max_rounds: int | None = None,
    n_per_provider: int | None = None,
    dock_enabled: bool = True,
    use_mock: bool = False,
    verbose: bool = True,
    hitl: bool = False,
) -> dict:
    """Run the iterative loop. Returns summary dict.

    Phase 4.1: integrates WorkingMemory + FailedLigandSet + LoopController + HITLCheckpoint.
    """
    config = copy.deepcopy(config)
    loop_cfg_dict = config.get("loop", {}) or {}
    if max_rounds is None:
        max_rounds = int(loop_cfg_dict.get("max_rounds", 5))
    if n_per_provider is None:
        n_per_provider = int(loop_cfg_dict.get("candidates_per_round_per_generator", 5))
    if max_rounds < 1 or n_per_provider < 1:
        raise ValueError("max_rounds and n_per_provider must be positive")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    out_path = Path(output_dir)
    if any(out_path.glob("round_*.json")) or (out_path / "manifest.json").exists():
        raise FileExistsError("Output already contains a run; use a new output directory")
    out_path.mkdir(parents=True, exist_ok=True)

    target = config["target"]
    scoring = config["scoring"]
    llm_cfg = config["llm"]
    provider_names = llm_cfg.get("generators", [])
    if target.get("name") != "EGFR":
        raise ValueError("Current prompts are EGFR-specific; other targets are not supported yet")
    if not use_mock:
        from agents.llm import get_client
        for provider in set(provider_names + [llm_cfg.get("judge", "MiniMax")]):
            get_client(provider, config, mock=False)  # credential/config preflight, no API call
    if dock_enabled:
        validate_receptor(target["receptor_pdbqt"])
    protocol = evaluation_protocol(target, scoring, dock_enabled)
    protocol_id = digest(protocol)
    memory_namespace = str(loop_cfg_dict.get("memory_namespace", "default"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", memory_namespace):
        raise ValueError("loop.memory_namespace may contain only letters, digits, ., _, and -")
    if use_mock:
        memory_base = out_path / "_memory"
        evaluation_cache_root = out_path / "_evaluation_cache"
        docking_cache_root = out_path / "_docking_cache"
    else:
        memory_base = (
            Path(loop_cfg_dict.get("memory_dir", "memory/v2"))
            / target["name"] / protocol_id / memory_namespace
        )
        evaluation_cache_root = Path(
            loop_cfg_dict.get("evaluation_cache_dir", "memory/evaluation_cache")
        )
        docking_cache_root = Path(
            loop_cfg_dict.get("docking_cache_dir", "memory/docking_cache")
        )
    evaluation_cache = EvaluationCache(
        evaluation_cache_root,
        protocol_id,
        enabled=bool(loop_cfg_dict.get("evaluation_cache_enabled", True)),
    )
    docking_protocol_id = digest(docking_protocol(target, scoring))
    docking_cache = DockingCache(
        docking_cache_root,
        docking_protocol_id,
        enabled=bool(loop_cfg_dict.get("docking_cache_enabled", True)),
    )
    judge_enabled = bool(loop_cfg_dict.get("judge_enabled", True))
    memory_enabled = bool(loop_cfg_dict.get("memory_enabled", True))
    failed_set_enabled = bool(loop_cfg_dict.get("failed_set_enabled", memory_enabled))
    require_all_generators = bool(loop_cfg_dict.get("require_all_generators", False))
    generation_max_attempts = int(loop_cfg_dict.get("generation_max_attempts", 1))
    judge_max_attempts = int(loop_cfg_dict.get("judge_max_attempts", 1))
    if generation_max_attempts < 1 or judge_max_attempts < 1:
        raise ValueError("loop generation/judge max attempts must be positive")
    # 2026-09-17: which per-round number the patience counter watches.
    # "safe_vina" (preferred for real experiments) only counts Vina progress
    # made by candidates that pass the safety gate; "vina" is the legacy
    # all-candidates signal that let a high-hERG molecule count as progress.
    # See docs/REVIEW_MINIMAX_ADVICE_20260917.md.
    progress_signal = str(loop_cfg_dict.get("progress_signal", "vina"))
    if progress_signal not in ("vina", "safe_vina"):
        raise ValueError("loop.progress_signal must be 'vina' or 'safe_vina'")
    manifest = {"schema_version": 2, "run_id": run_id, "is_mock": use_mock,
                "protocol_id": protocol_id, "protocol": protocol,
                "execution": {
                    "max_rounds": max_rounds,
                    "candidates_per_round_per_generator": n_per_provider,
                    "providers": provider_names,
                    "dock_enabled": dock_enabled,
                    "memory_namespace": memory_namespace,
                    "judge_enabled": judge_enabled,
                    "memory_enabled": memory_enabled,
                    "failed_set_enabled": failed_set_enabled,
                    "progress_signal": progress_signal,
                    "progress_patience": loop_cfg_dict.get("early_stop_patience", 3),
                    "require_all_generators": require_all_generators,
                    "generation_max_attempts": generation_max_attempts,
                    "judge_max_attempts": judge_max_attempts,
                    "evaluation_cache_enabled": evaluation_cache.enabled,
                    "evaluation_cache_dir": str(evaluation_cache.directory.resolve()),
                    "docking_cache_enabled": docking_cache.enabled,
                    "docking_protocol_id": docking_protocol_id,
                    "docking_cache_dir": str(docking_cache.directory.resolve()),
                },
                "llm": {name: {k: v for k, v in settings.items() if k != "api_key_env" and "key" not in k.lower()}
                        for name, settings in llm_cfg.get("providers", {}).items()},
                "reference_registry_sha256": file_hash("data/reference_compounds.json"),
                "sa_fragment_model_sha256": file_hash("tools/fpscores.pkl.gz"),
                "code_hashes": {str(p): file_hash(p) for p in
                                [Path("loop.py"), *Path("agents").glob("*.py"), *Path("tools").glob("*.py")]}}
    (out_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ----- Phase 4.1 modules -----
    loop_controller = LoopController(LoopConfig(
        max_rounds=max_rounds,
        token_budget=loop_cfg_dict.get("token_budget", 50000),
        judge_convergence_patience=loop_cfg_dict.get(
            "early_stop_patience",
            loop_cfg_dict.get("judge_convergence_patience", 2),
        ),
        progress_signal=progress_signal,
    ))
    state = LoopState()
    # Phase 4.3 (P0-3 / P1-1): persistence paths for strategy history + best molecules
    memory_strategy_path = memory_base / "strategy_history.json"
    memory_best_path = memory_base / "best_molecules.json"
    memory = WorkingMemory(
        max_recent=loop_cfg_dict.get("memory_max_recent", 3),
        strategy_persist_path=memory_strategy_path if memory_enabled else None,
        best_persist_path=memory_best_path if memory_enabled else None,
        target_name=target["name"],
    )
    # Phase 4.3 (P2-1 fix): read thresholds from scoring.failed_set with
    # backward-compat fallback to loop.failed_* (legacy keys).
    failed_cfg = (scoring or {}).get("failed_set", {}) or {}
    failed_set = FailedLigandSet(
        path=(memory_base / "failed_ligands.json" if failed_set_enabled else
              out_path / "_disabled_failed_ligands.json"),
        threshold_composite=loop_cfg_dict.get(
            "failed_threshold_composite",
            failed_cfg.get("composite_floor", 0.5),
        ),
        threshold_vina=loop_cfg_dict.get(
            "failed_threshold_vina",
            failed_cfg.get("vina_floor", -2.5),
        ),
        max_size=loop_cfg_dict.get(
            "failed_max_size",
            failed_cfg.get("max_size", 0),
        ),
        # Optional similarity signal. The loop decides whether it is report-only
        # or exclusionary via scoring.failed_set.embeddings.mode.
        enable_embeddings=failed_set_enabled and bool(
            (failed_cfg.get("embeddings") or {}).get("enabled", False)
        ),
        embedding_threshold=float(
            (failed_cfg.get("embeddings") or {}).get("threshold", 0.85)
        ),
        embedding_model=str(
            (failed_cfg.get("embeddings") or {}).get("model", "all-MiniLM-L6-v2")
        ),
        embedding_device=str(
            (failed_cfg.get("embeddings") or {}).get("device", "auto")
        ),
    )
    hitl_cp = HITLCheckpoint(require_approval=hitl)

    rounds_log = []
    summary_history = []
    enriched_history: list[list[dict]] = []  # Phase 4.3 (P1-4): keep last round's enriched list
    focus = ""
    weakness = ""
    stop_reason = "max_rounds_reached"

    # ----- Pre-loop HITL checkpoint -----
    if hitl:
        cont = hitl_cp.pre_loop(max_rounds, n_per_provider, len(provider_names))
        if not cont:
            return {"target": target["name"], "rounds_completed": 0,
                    "history": [], "final_focus": "(user cancelled)"}

    for round_num in range(max_rounds):
        # ----- LoopController: should we stop before starting next round? -----
        state.round = round_num
        stop, reason = loop_controller.should_stop(state)
        if stop:
            stop_reason = reason
            if verbose:
                print(f"\n[STOP] {reason}: {loop_controller.explain(reason)}")
            break

        if verbose:
            mem_ctx = (
                memory.compress_for_generator()
                if memory_enabled else "Memory disabled for this experiment."
            )
            print(f"\n=== Round {round_num} ===")
            print(f"  [memory] {mem_ctx.splitlines()[0]}")
            print(f"  [focus] {focus[:80] if focus else '(initial)'}")

        focus_used = focus
        previous_summary = summary_history[-1] if summary_history else None
        previous_enriched = enriched_history[-1] if enriched_history else None

        # ----- Agent A: generate (with WorkingMemory + FailedLigandSet) -----
        if verbose:
            print(f"  [A] generating with {provider_names}...")
        gen_results = generate_candidates(
            config=config,
            providers=provider_names,
            n_per_provider=n_per_provider,
            focus=focus,
            weakness=weakness,
            # Phase 4.1: pass memory context + failed-smiles prompt
            memory_context=(memory.compress_for_generator() if memory_enabled else ""),
            failed_prompt=(failed_set.format_for_prompt() if failed_set_enabled else ""),
            use_mock=use_mock,
            max_attempts_per_provider=generation_max_attempts,
        )
        (out_path / f"proposals_{round_num}.json").write_text(
            json.dumps(gen_results, indent=2, ensure_ascii=False), encoding="utf-8")
        state.tokens_used += sum(r.get("usage", {}).get("total_tokens", 0) or 0 for r in gen_results)

        if require_all_generators:
            outputs_by_provider = {result.get("provider"): result for result in gen_results}
            generation_errors = []
            for provider in provider_names:
                output = outputs_by_provider.get(provider)
                if output is None:
                    generation_errors.append(f"{provider}:missing")
                    continue
                if output.get("error"):
                    generation_errors.append(f"{provider}:error={output['error']}")
                actual = len(output.get("smiles_list") or [])
                if actual != n_per_provider:
                    generation_errors.append(
                        f"{provider}:candidates={actual} expected={n_per_provider}"
                    )
            if generation_errors:
                stop_reason = "generation_incomplete"
                if verbose:
                    print(f"  [!] incomplete generator batch: {'; '.join(generation_errors)}")
                break

        candidates = flatten_generator_results(gen_results)
        embedding_cfg = (failed_cfg.get("embeddings") or {})
        candidates, candidate_filter = filter_candidates_for_evaluation(
            candidates,
            failed_set,
            similarity_mode=str(embedding_cfg.get("mode", "report")),
        )
        for i, candidate in enumerate(candidates):
            candidate.update(candidate_id=f"{run_id}:r{round_num}:c{i}", run_id=run_id,
                             round=round_num, focus_used=focus_used, is_mock=use_mock)
        if verbose:
            print(
                f"  [A] kept {len(candidates)}/{candidate_filter['raw']} candidates "
                f"after exact deduplication"
            )

        if not candidates:
            stop_reason = "no_candidates"
            if verbose:
                print(f"  [!] no candidates generated; stopping loop")
            break

        # ----- Agent B: evaluate -----
        if verbose:
            print(f"  [B] evaluating with 4 tools (dock={dock_enabled})...")
        enriched, evaluation_cache_stats = evaluate_candidates_with_cache(
            candidates=candidates,
            scoring_config=scoring,
            target_config=target,
            dock_enabled=dock_enabled,
            artifact_dir=str(out_path / "artifacts"),
            cache=evaluation_cache,
            docking_cache=docking_cache,
        )

        # ----- Round summary -----
        summary = summarize_round(enriched)
        summary["round"] = round_num
        summary["candidate_filter"] = candidate_filter
        summary["evaluation_cache"] = evaluation_cache_stats
        summary_history.append(summary)
        enriched_history.append(enriched)  # Phase 4.3 (P1-4): keep for next-round Judge
        if verbose:
            print(f"  [B] valid={summary['n_valid']}/{summary['n_total']} "
                  f"avg_ADMET={summary['avg_admet']} "
                  f"unique_scaffolds={summary['n_unique_scaffolds']} "
                  f"best_Vina={summary['best_vina']} "
                  f"best_safe_Vina={summary['best_safe_vina']} "
                  f"(safe {summary['n_docked_safe']}/{summary['n_docked']} docked)")
            print(
                f"  [cache] hits={evaluation_cache_stats['hits']} "
                f"misses={evaluation_cache_stats['misses']} "
                f"stored={evaluation_cache_stats['stored']}"
            )
            if dock_enabled:
                print(
                    f"  [dock-cache] hits={evaluation_cache_stats['docking_hits']} "
                    f"misses={evaluation_cache_stats['docking_misses']} "
                    f"stored={evaluation_cache_stats['docking_stored']}"
                )

        # ----- Agent C: judge -----
        if verbose:
            print("  [C] judging round..." if judge_enabled else "  [C] judge disabled")
        # Phase 4.2: pass previous focus + summary for self-reflection
        if judge_enabled:
            judgment_errors = []
            judgment_tokens = 0
            for judge_attempt in range(1, judge_max_attempts + 1):
                judgment = judge_round(
                    enriched, config, round_num,
                    previous_focus=focus,             # focus from prior round (empty on round 0)
                    previous_summary=previous_summary,
                    previous_enriched=previous_enriched,  # full prior candidate list
                    use_mock=use_mock,
                )
                judgment_tokens += judgment.get("usage", {}).get("total_tokens", 0) or 0
                if judgment.get("status") == "ok":
                    break
                judgment_errors.append(
                    str((judgment.get("summary") or {}).get("error") or "judge_error")
                )
            judgment["attempt_count"] = judge_attempt
            if judgment_errors:
                key = "prior_attempt_errors" if judgment.get("status") == "ok" else "attempt_errors"
                judgment[key] = judgment_errors
        else:
            judgment = {
                "status": "disabled",
                "focus": "",
                "weakness": "",
                "reflection": "",
                "confidence": 0.0,
                "adopted_count": 0,
                "adoption_denominator": 0,
                "usage": {},
            }
            judgment_tokens = 0
        state.tokens_used += judgment_tokens
        focus = judgment["focus"]
        weakness = judgment.get("weakness", "")
        reflection = judgment.get("reflection", "")
        confidence = judgment.get("confidence", 0.0)
        adopted_count = judgment.get("adopted_count", 0)
        if verbose and judge_enabled:
            print(f"  [C] weakness: {weakness[:80]}")
            print(f"  [C] next focus: {focus[:120]}")
            if reflection:
                print(f"  [C] reflection: {reflection[:120]}  (confidence={confidence:.2f})")
            if adopted_count:
                print(f"  [C] adopted previous focus in {adopted_count}/{summary['n_valid']} molecules")

        # ----- Per-provider diversity (heterogeneous generation evidence) -----
        per_provider = _per_provider_stats(enriched)
        summary["per_provider"] = per_provider
        if verbose and per_provider:
            for prov, st in per_provider.items():
                if prov == "__overlap__":
                    continue
                print(f"  [D] {prov}: {st['count']} mols, "
                      f"{st['n_unique_scaffolds']} unique scaffolds, "
                      f"best_Vina={st['best_vina']}")
            if "__overlap__" in per_provider:
                ov = per_provider["__overlap__"]
                print(f"  [D] scaffold overlap ({ov['providers'][0]} vs "
                      f"{ov['providers'][1]}): {ov['overlap_ratio']} "
                      f"({ov['intersection']}/{ov['union']})")

        # ----- Phase 4.1: WorkingMemory.update -----
        if memory_enabled:
            memory.add_round(enriched, focus_used)

        # ----- Phase 4.1: FailedLigandSet update -----
        new_failures = []
        for c in enriched:
            safety_failed = c.get("safety_gate_pass") is False
            if failed_set_enabled and c.get("evaluation_status") == "complete" and not use_mock and (
                safety_failed or failed_set.should_mark_failed(
                    c["composite_score"], c["dock"].get("score")
                )
            ):
                reason = (
                    f"composite={c['composite_score']:.2f}, "
                    f"vina={c['dock'].get('score')}, "
                    f"safety_gate_pass={c.get('safety_gate_pass')}"
                )
                new_failures.append((c["smiles"], reason))
        if new_failures:
            failed_set.add_failed_many(new_failures)
        if verbose and failed_set_enabled and failed_set.failed:
            print(f"  [memory] failed_set now holds {len(failed_set.failed)} SMILES")

        # ----- LoopController: track both progress signals -----
        # Both counters are always maintained, so a saved run can be re-audited
        # under either termination policy. Only the configured one stops the loop.
        round_best_vina = summary.get("best_vina")
        round_best_safe_vina = summary.get("best_safe_vina")
        prior_best = state.best_vina
        prior_best_safe = state.best_safe_vina
        state.note_round_result(round_best_vina)
        state.note_round_safe_result(round_best_safe_vina)
        if verbose:
            print(f"  [controller] signal={progress_signal} "
                  f"patience={loop_controller.config.judge_convergence_patience} "
                  f"no_improvement={loop_controller.rounds_without_improvement(state)} "
                  f"(vina={state.rounds_without_vina_improvement}, "
                  f"safe_vina={state.rounds_without_safe_vina_improvement})")

        # HITL reports a "breakthrough" on whichever signal the loop is optimising.
        hitl_prior = prior_best_safe if progress_signal == "safe_vina" else prior_best
        hitl_now = round_best_safe_vina if progress_signal == "safe_vina" else round_best_vina
        if hitl and hitl_prior is not None and hitl_now is not None:
            if hitl_now < hitl_prior:
                state.hitl_veto = not hitl_cp.on_vina_breakthrough(hitl_prior, hitl_now)

        # ----- Save round JSON -----
        round_record = {
            "round": round_num,
            "run_id": run_id,
            "protocol_id": protocol_id,
            "is_mock": use_mock,
            "focus_used": focus_used,
            "focus_next": focus,
            "previous_summary": previous_summary,
            "tokens_used": state.tokens_used,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "target": target["name"],
            "focus": focus,
            "generator_outputs": gen_results,
            "candidates": enriched,
            "summary": summary,
            "judgment": judgment,
            "memory_snapshot": {
                "recent_rounds": [
                    {
                        "round": r.round,
                        "n_valid": r.n_valid,
                        "best_vina": r.best_vina,
                        "n_unique_scaffolds": r.n_unique_scaffolds,
                    }
                    for r in memory.recent_rounds
                ],
                "best_so_far": memory.best_so_far.get("smiles") if memory.best_so_far else None,
            },
            "loop_state": {
                "round": state.round,
                "progress_signal": progress_signal,
                "rounds_without_vina_improvement": state.rounds_without_vina_improvement,
                "rounds_without_safe_vina_improvement": state.rounds_without_safe_vina_improvement,
            },
        }
        round_file = out_path / f"round_{round_num}.json"
        round_file.write_text(
            json.dumps(round_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if verbose:
            print(f"  [save] {round_file}")

        rounds_log.append(round_record)
        if judgment.get("status") == "error":
            stop_reason = "judge_error"
            break  # preserve evidence, do not execute an invented fallback strategy

        # ----- Phase 4.1 bugfix: also check stop AFTER a round, not only before.
        # If the round that just finished pushed us past the patience threshold
        # (or token budget, or HITL veto), stop now instead of waiting for the
        # next round's pre-check.
        post_stop, post_reason = loop_controller.should_stop(state)
        if post_stop:
            stop_reason = post_reason
            if verbose:
                print(f"  [controller] stop after round {round_num}: "
                      f"{loop_controller.explain(post_reason)}")
            break

    # ----- Phase 4.1: End-of-loop HITL candidate selection -----
    if hitl and rounds_log:
        # Collect top candidates across all rounds
        all_top = []
        for r in rounds_log:
            for c in r.get("candidates", []):
                if c.get("evaluation_status") == "complete":
                    all_top.append(c)
        all_top.sort(key=lambda c: c.get("composite_score", 0), reverse=True)
        chosen = hitl_cp.select_synthesis_candidates(all_top[:5])
        if verbose:
            print(f"\n[HITL] synthesis whitelist: {len(chosen)} molecules")

    # ----- Final summary -----
    overall = {
        "run_id": run_id, "protocol_id": protocol_id, "is_mock": use_mock,
        "output_dir": str(out_path.resolve()), "tokens_used": state.tokens_used,
        "status": "error" if stop_reason in (
            "judge_error", "no_candidates", "generation_incomplete"
        ) else "finished",
        "stop_reason": stop_reason,
        "target": target["name"],
        "rounds_completed": len(rounds_log),
        "history": summary_history,
        "final_focus": focus,
        "loop_state": {
            "progress_signal": progress_signal,
            "rounds_without_vina_improvement": state.rounds_without_vina_improvement,
            "rounds_without_safe_vina_improvement": state.rounds_without_safe_vina_improvement,
            "best_vina": state.best_vina,
            "best_safe_vina": state.best_safe_vina,
            "hitl_veto": state.hitl_veto,
            "failed_set_size": len(failed_set.failed),
            "memory_rounds": len(memory.recent_rounds),
        },
    }

    # ----- Phase 4.3 (P2-3): aggregate agent-level metrics -----
    judgments = [r.get("judgment", {}) for r in rounds_log]
    metrics = compute_agent_metrics(
        summary_history=summary_history,
        judgments=judgments,
        loop_state=overall["loop_state"],
    )
    overall["agent_metrics"] = {
        "best_vina_first": metrics["aggregates"]["best_vina_first"],
        "best_vina_last": metrics["aggregates"]["best_vina_last"],
        "best_vina_delta": metrics["aggregates"]["best_vina_delta"],
        "valid_rate_improvement": metrics["aggregates"]["valid_rate_improvement"],
        "adoption_rate_avg_llm": metrics["aggregates"]["adoption_rate_avg_llm"],
        "adoption_rate_avg_det": metrics["aggregates"]["adoption_rate_avg_det"],
        "adoption_llm_vs_det_drift_avg": metrics["aggregates"]["adoption_llm_vs_det_drift_avg"],
        "run_shows_improvement": metrics["verdict"]["run_shows_improvement"],
        "agent_is_learning": metrics["verdict"]["agent_is_learning"],
        "verdict_rationale": metrics["verdict"]["rationale"],
        "see_also": "metrics.json",
    }
    (out_path / "summary.json").write_text(
        json.dumps(overall, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Full metrics dump (curves + verdict) goes to metrics.json
    (out_path / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    if verbose:
        v = metrics["aggregates"]
        print(
            f"\n[metrics] best_vina {v['best_vina_first']} -> {v['best_vina_last']} "
            f"(delta={v['best_vina_delta']}); "
            f"run_improved={metrics['verdict']['run_shows_improvement']}"
        )
        print(
            f"[metrics] adoption LLM={v['adoption_rate_avg_llm']} "
            f"det={v['adoption_rate_avg_det']} drift={v['adoption_llm_vs_det_drift_avg']}"
        )
    return overall


def _per_provider_stats(enriched: list[dict]) -> dict:
    """Per-provider summary: count, unique scaffolds, best Vina, scaffold overlap."""
    from collections import defaultdict
    by_prov: dict[str, list[dict]] = defaultdict(list)
    for c in enriched:
        if c["validate"].get("valid"):
            by_prov[c.get("provider", "?")].append(c)
    stats = {}
    for prov, cs in by_prov.items():
        scafs = {c["scaffold"] for c in cs if c.get("scaffold")}
        vina_scores = [c["dock"]["score"] for c in cs
                       if c["dock"].get("score") is not None]
        stats[prov] = {
            "count": len(cs),
            "n_unique_scaffolds": len(scafs),
            "best_vina": min(vina_scores) if vina_scores else None,
            "avg_composite": round(
                sum(c["composite_score"] for c in cs if c.get("composite_score") is not None) / max(sum(c.get("composite_score") is not None for c in cs), 1), 3) if any(c.get("composite_score") is not None for c in cs) else None,
        }
    # Pairwise scaffold overlap (Tanimoto of scaffold sets)
    providers = list(by_prov.keys())
    if len(providers) >= 2:
        a_scafs = {c["scaffold"] for c in by_prov[providers[0]] if c.get("scaffold")}
        b_scafs = {c["scaffold"] for c in by_prov[providers[1]] if c.get("scaffold")}
        if a_scafs or b_scafs:
            inter = len(a_scafs & b_scafs)
            union = len(a_scafs | b_scafs) or 1
            stats["__overlap__"] = {
                "providers": providers,
                "intersection": inter,
                "union": len(a_scafs | b_scafs),
                "overlap_ratio": round(inter / union, 3),
            }
    return stats


def main():
    p = argparse.ArgumentParser(description="AIDD Multi-Agent Loop")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--output", default=None,
                   help="base output directory (default: loop.output_dir in config)")
    p.add_argument("--rounds", type=int, default=None,
                   help="override loop.max_rounds from config")
    p.add_argument("--n", type=int, default=None,
                   help="override candidates_per_round_per_generator from config")
    p.add_argument("--no-dock", action="store_true", help="skip Vina (faster)")
    p.add_argument("--mock", action="store_true", help="use mock LLM (no API key needed)")
    p.add_argument("--hitl", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    config = load_config(args.config)
    loop_cfg = config.get("loop", {}) or {}
    base_output = args.output or loop_cfg.get("output_dir", "runs")
    overall = run_loop(
        config=config,
        output_dir=str(Path(base_output) / (("mock_" if args.mock else "run_") + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])),
        max_rounds=args.rounds,
        n_per_provider=args.n,
        dock_enabled=not args.no_dock,
        use_mock=args.mock,
        verbose=not args.quiet,
        hitl=args.hitl,
    )
    print(f"\n[OK] loop finished: {overall['rounds_completed']} rounds -> {overall.get('output_dir', base_output)}")
    print(f"     run_id: {overall.get('run_id')}")


if __name__ == "__main__":
    main()
