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
import uuid
from tools.provenance import digest, file_hash, evaluation_protocol
from tools.dock_score import validate_receptor
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from agents.generator import generate_candidates
from agents.evaluator import evaluate_candidates, summarize_round
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


def run_loop(
    config: dict,
    output_dir: str = "runs",
    max_rounds: int = 5,
    n_per_provider: int = 5,
    dock_enabled: bool = True,
    use_mock: bool = False,
    verbose: bool = True,
    hitl: bool = False,
) -> dict:
    """Run the iterative loop. Returns summary dict.

    Phase 4.1: integrates WorkingMemory + FailedLigandSet + LoopController + HITLCheckpoint.
    """
    config = copy.deepcopy(config)
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
        for provider in set(provider_names + [llm_cfg.get("judge", "deepseek")]):
            get_client(provider, config, mock=False)  # credential/config preflight, no API call
    if dock_enabled:
        validate_receptor(target["receptor_pdbqt"])
    protocol = evaluation_protocol(target, scoring, dock_enabled)
    protocol_id = digest(protocol)
    manifest = {"schema_version": 2, "run_id": run_id, "is_mock": use_mock,
                "protocol_id": protocol_id, "protocol": protocol,
                "llm": {name: {k: v for k, v in settings.items() if k != "api_key_env" and "key" not in k.lower()}
                        for name, settings in llm_cfg.get("providers", {}).items()},
                "reference_registry_sha256": file_hash("data/reference_compounds.json"),
                "sa_fragment_model_sha256": file_hash("tools/fpscores.pkl.gz"),
                "code_hashes": {str(p): file_hash(p) for p in
                                [Path("loop.py"), *Path("agents").glob("*.py"), *Path("tools").glob("*.py")]}}
    (out_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ----- Phase 4.1 modules -----
    loop_cfg_dict = config.get("loop", {})
    loop_controller = LoopController(LoopConfig(
        max_rounds=max_rounds,
        token_budget=loop_cfg_dict.get("token_budget", 50000),
        judge_convergence_patience=loop_cfg_dict.get("judge_convergence_patience", 2),
    ))
    state = LoopState()
    # Phase 4.3 (P0-3 / P1-1): persistence paths for strategy history + best molecules
    memory_strategy_path = (
        Path("memory/strategy_history") / target["name"] / (protocol_id + ".json")
    )
    memory_best_path = Path("memory/best_molecules.json")
    memory = WorkingMemory(
        max_recent=loop_cfg_dict.get("memory_max_recent", 3),
        strategy_persist_path=memory_strategy_path,
        best_persist_path=memory_best_path,
        target_name=target["name"],
    )
    # Phase 4.3 (P2-1 fix): read thresholds from scoring.failed_set with
    # backward-compat fallback to loop.failed_* (legacy keys).
    failed_cfg = (scoring or {}).get("failed_set", {}) or {}
    failed_set = FailedLigandSet(
        path=(out_path / "mock_memory.json" if use_mock else
              Path("memory/v2") / target["name"] / (protocol_id + ".json")),
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
            mem_ctx = memory.compress_for_generator()
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
            memory_context=memory.compress_for_generator(),
            failed_prompt=failed_set.format_for_prompt(),
            use_mock=use_mock,
        )
        candidates = flatten_generator_results(gen_results)
        for i, candidate in enumerate(candidates):
            candidate.update(candidate_id=f"{run_id}:r{round_num}:c{i}", run_id=run_id,
                             round=round_num, focus_used=focus_used, is_mock=use_mock)
        (out_path / f"proposals_{round_num}.json").write_text(
            json.dumps(gen_results, indent=2, ensure_ascii=False), encoding="utf-8")
        state.tokens_used += sum(r.get("usage", {}).get("total_tokens", 0) or 0 for r in gen_results)
        if verbose:
            print(f"  [A] got {len(candidates)} raw candidates from {len(gen_results)} providers")

        if not candidates:
            stop_reason = "no_candidates"
            if verbose:
                print(f"  [!] no candidates generated; stopping loop")
            break

        # ----- Agent B: evaluate -----
        if verbose:
            print(f"  [B] evaluating with 4 tools (dock={dock_enabled})...")
        enriched = evaluate_candidates(
            candidates=candidates,
            scoring_config=scoring,
            target_config=target,
            dock_enabled=dock_enabled,
            artifact_dir=str(out_path / "artifacts"),
        )

        # ----- Round summary -----
        summary = summarize_round(enriched)
        summary["round"] = round_num
        summary_history.append(summary)
        enriched_history.append(enriched)  # Phase 4.3 (P1-4): keep for next-round Judge
        if verbose:
            print(f"  [B] valid={summary['n_valid']}/{summary['n_total']} "
                  f"avg_ADMET={summary['avg_admet']} "
                  f"unique_scaffolds={summary['n_unique_scaffolds']} "
                  f"best_Vina={summary['best_vina']}")

        # ----- Agent C: judge -----
        if verbose:
            print(f"  [C] judging round...")
        # Phase 4.2: pass previous focus + summary for self-reflection
        judgment = judge_round(
            enriched, config, round_num,
            previous_focus=focus,             # focus from prior round (empty on round 0)
            previous_summary=previous_summary,
            previous_enriched=previous_enriched,  # Phase 4.3 (P1-4): full prior candidate list
            use_mock=use_mock,
        )
        state.tokens_used += judgment.get("usage", {}).get("total_tokens", 0) or 0
        focus = judgment["focus"]
        weakness = judgment.get("weakness", "")
        reflection = judgment.get("reflection", "")
        confidence = judgment.get("confidence", 0.0)
        adopted_count = judgment.get("adopted_count", 0)
        if verbose:
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
        memory.add_round(enriched, focus_used)

        # ----- Phase 4.1: FailedLigandSet update -----
        for c in enriched:
            if c.get("evaluation_status") == "complete" and not use_mock and failed_set.should_mark_failed(
                c["composite_score"], c["dock"].get("score")
            ):
                reason = (
                    f"composite={c['composite_score']:.2f}, "
                    f"vina={c['dock'].get('score')}"
                )
                failed_set.add_failed(c["smiles"], reason)
        if verbose and failed_set.failed:
            print(f"  [memory] failed_set now holds {len(failed_set.failed)} SMILES")

        # ----- LoopController: track best Vina -----
        round_best_vina = summary.get("best_vina")
        prior_best = state.best_vina
        state.note_round_result(round_best_vina)
        if verbose:
            print(f"  [controller] rounds_without_improvement = "
                  f"{state.rounds_without_vina_improvement}")

        if hitl and prior_best is not None and round_best_vina is not None:
            if round_best_vina < prior_best:
                state.hitl_veto = not hitl_cp.on_vina_breakthrough(prior_best, round_best_vina)

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
                "rounds_without_vina_improvement": state.rounds_without_vina_improvement,
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
        "status": "error" if stop_reason in ("judge_error", "no_candidates") else "finished",
        "stop_reason": stop_reason,
        "target": target["name"],
        "rounds_completed": len(rounds_log),
        "history": summary_history,
        "final_focus": focus,
        "loop_state": {
            "rounds_without_vina_improvement": state.rounds_without_vina_improvement,
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
            f"(delta={v['best_vina_delta']}); learning={metrics['verdict']['agent_is_learning']}"
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
    p.add_argument("--output", default="runs")
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--n", type=int, default=5, help="candidates per provider per round")
    p.add_argument("--no-dock", action="store_true", help="skip Vina (faster)")
    p.add_argument("--mock", action="store_true", help="use mock LLM (no API key needed)")
    p.add_argument("--hitl", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    config = load_config(args.config)
    overall = run_loop(
        config=config,
        output_dir=str(Path(args.output) / (("mock_" if args.mock else "run_") + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])),
        max_rounds=args.rounds,
        n_per_provider=args.n,
        dock_enabled=not args.no_dock,
        use_mock=args.mock,
        verbose=not args.quiet,
        hitl=args.hitl,
    )
    print(f"\n[OK] loop finished: {overall['rounds_completed']} rounds -> {overall.get('output_dir', args.output)}")
    print(f"     run_id: {overall.get('run_id')}")


if __name__ == "__main__":
    main()