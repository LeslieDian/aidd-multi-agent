"""loop.py - Main iterative loop for AIDD Multi-Agent.

Usage:
    # Real mode (requires .env with API keys)
    python loop.py

    # Mock mode (no API keys needed, uses pre-set molecules)
    python loop.py --mock

    # Custom target / rounds
    python loop.py --rounds 5 --output runs/ --mock

Output: runs/round_0.json ... runs/round_{n-1}.json + summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from agents.generator import generate_candidates
from agents.evaluator import evaluate_candidates, summarize_round
from agents.judge import judge_round


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
) -> dict:
    """Run the iterative loop. Returns summary dict."""
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    target = config["target"]
    scoring = config["scoring"]
    llm_cfg = config["llm"]
    provider_names = llm_cfg.get("generators", [])

    rounds_log = []
    summary_history = []
    focus = ""
    weakness = ""

    for round_num in range(max_rounds):
        if verbose:
            print(f"\n=== Round {round_num} ===" + (f"  (focus: {focus})" if focus else ""))

        # ----- Agent A: generate -----
        if verbose:
            print(f"  [A] generating with {provider_names}...")
        gen_results = generate_candidates(
            config=config,           # full config so get_client can find providers
            providers=provider_names,
            n_per_provider=n_per_provider,
            focus=focus,
            weakness=weakness,
            use_mock=use_mock,
        )
        candidates = flatten_generator_results(gen_results)
        if verbose:
            print(f"  [A] got {len(candidates)} raw candidates from {len(gen_results)} providers")

        if not candidates:
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
        )

        # ----- Round summary -----
        summary = summarize_round(enriched)
        summary["round"] = round_num
        summary_history.append(summary)
        if verbose:
            print(f"  [B] valid={summary['n_valid']}/{summary['n_total']} "
                  f"avg_ADMET={summary['avg_admet']:.3f} "
                  f"unique_scaffolds={summary['n_unique_scaffolds']} "
                  f"best_Vina={summary['best_vina']}")

        # ----- Agent C: judge -----
        if verbose:
            print(f"  [C] judging round...")
        judgment = judge_round(enriched, config, round_num, use_mock=use_mock)
        focus = judgment["focus"]
        weakness = judgment.get("weakness", "")
        if verbose:
            print(f"  [C] weakness: {weakness[:80]}")
            print(f"  [C] next focus: {focus[:120]}")

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

        # ----- Save round JSON -----
        round_record = {
            "round": round_num,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "target": target["name"],
            "focus": focus,
            "generator_outputs": gen_results,
            "candidates": enriched,
            "summary": summary,
            "judgment": judgment,
        }
        round_file = out_path / f"round_{round_num}.json"
        round_file.write_text(
            json.dumps(round_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if verbose:
            print(f"  [save] {round_file}")

        rounds_log.append(round_record)

        # ----- Early stop -----
        if (round_num > 0
            and summary["n_unique_scaffolds"] == 0
            and summary["avg_admet"] <= summary_history[-2]["avg_admet"]):
            if verbose:
                print(f"  [early stop] no improvement in round {round_num}")
            break

    # ----- Final summary -----
    overall = {
        "target": target["name"],
        "rounds_completed": len(rounds_log),
        "history": summary_history,
        "final_focus": focus,
    }
    (out_path / "summary.json").write_text(
        json.dumps(overall, indent=2, ensure_ascii=False),
        encoding="utf-8",
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
                sum(c["composite_score"] for c in cs) / max(len(cs), 1), 3),
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
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    config = load_config(args.config)
    overall = run_loop(
        config=config,
        output_dir=args.output,
        max_rounds=args.rounds,
        n_per_provider=args.n,
        dock_enabled=not args.no_dock,
        use_mock=args.mock,
        verbose=not args.quiet,
    )
    print(f"\n[OK] loop finished: {overall['rounds_completed']} rounds -> {args.output}/")
    print(f"     summary: {args.output}/summary.json")


if __name__ == "__main__":
    main()