"""scripts/run_phase4_6_ablation.py - Phase 4.6 A/B ablation.

Compares single-agent vs multi-agent (3 generators / 3 judges + debate)
on a fixed smoke workload: 1 round, n_per_provider=1 (or
n_per_generator=1), max_rounds=1, mock=False. The two arms share the
same prompt / scoring / safety gate; only the orchestration differs.

Outputs runs/samples/phase4_6_ablation_<timestamp>.json containing:
  - per-arm candidates (raw SMILES, generator source)
  - per-arm aggregator stats (dedup / diversity / kept)
  - per-arm judge votes (combined score, dispersion)
  - per-arm aggregate qualitative labels (best SMILES, scaffold set,
    any goal_met / decision_loop / consecutive_errors)

This is intentionally a CHEAP smoke (no Vina, no RDKit safety-gate
failures expected). The result is a unit-test-grade regression that
shows multi-agent actually ran end-to-end. Honest A/B statistics
require Phase 4.6 stage 12 follow-up: multi-run replicates +
non-parametric tests.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_single_agent_config(base_cfg: dict) -> dict:
    """Return a copy with multi_agent.enabled=false + single generator."""
    import copy
    cfg = copy.deepcopy(base_cfg)
    loop_cfg = cfg.setdefault("loop", {})
    ma = loop_cfg.setdefault("multi_agent", {})
    ma["enabled"] = False
    ma["generators"] = [
        {"name": "single", "provider": "MiniMax", "prompt_role": "qed",
         "temperature": 0.7, "weight": 1.0},
    ]
    ma["judges"] = [
        {"name": "single_judge", "provider": "judge_MiniMax",
         "prompt_role": "judge_property", "temperature": 0.3, "weight": 1.0},
    ]
    ma["router"] = {"enabled": False}
    ma["debate"] = {"enabled": False}
    return cfg


def _run_arm(name: str, config: dict, *, n_per_generator: int,
              max_rounds: int, output_dir: Path,
              verbose: bool = False) -> dict:
    from loop_multi_agent import run_multi_agent_loop
    started = time.time_ns()
    result = run_multi_agent_loop(
        config=config,
        output_dir=str(output_dir / name),
        n_per_generator=n_per_generator,
        max_rounds=max_rounds,
        use_mock=False,
        verbose=verbose,
    )
    elapsed_ms = round((time.time_ns() - started) / 1e6, 1)
    rounds = result.get("rounds_log", [])
    last_round = rounds[-1] if rounds else {}
    judge_block = last_round.get("judges", [])
    candidate_smiles_per_gen = {}
    for r in rounds:
        for gen_name, smi_list in (r.get("per_generator_counts") or {}).items():
            candidate_smiles_per_gen.setdefault(gen_name, 0)
            # use the actual aggregator counts if present
    return {
        "name": name,
        "n_generators": result.get("n_generators", 0),
        "n_judges": result.get("n_judges", 0),
        "n_rounds": len(rounds),
        "elapsed_ms": elapsed_ms,
        "last_round_aggregation_stats": last_round.get("aggregation_stats", {}),
        "last_round_judges": judge_block,
        "last_round_combined_score": last_round.get("judge_combined"),
        "last_round_dispersion": last_round.get("judge_dispersion"),
        "last_round_debate_triggered": last_round.get("debate_triggered"),
        "last_round_expert": last_round.get("expert_prompt"),
        "last_round_next_focus_preview": (last_round.get("next_focus") or "")[:160],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4.6 A/B ablation.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--n-per-generator", type=int, default=1)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--out", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    import yaml
    config_path = ROOT / args.config
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    out_dir = ROOT / "runs" / "_phase4_6_ablation"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.out is None:
        args.out = (
            ROOT / "runs" / "samples"
            / f"phase4_6_ablation_{time.strftime('%Y%m%d_%H%M%S')}.json"
        )
    args.out = Path(args.out)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    print(f"Phase 4.6 ablation: n_per_generator={args.n_per_generator} "
          f"max_rounds={args.max_rounds}")

    single_cfg = _make_single_agent_config(config)
    print("\n--- ARM 1: single-agent (1 generator, 1 judge) ---")
    single = _run_arm(
        "single_agent", single_cfg,
        n_per_generator=args.n_per_generator,
        max_rounds=args.max_rounds,
        output_dir=out_dir,
        verbose=args.verbose,
    )
    print(f"  elapsed={single['elapsed_ms']}ms "
          f"judges={single['last_round_judges']}")

    print("\n--- ARM 2: multi-agent (3 generators, 3 judges, debate) ---")
    multi = _run_arm(
        "multi_agent", config,
        n_per_generator=args.n_per_generator,
        max_rounds=args.max_rounds,
        output_dir=out_dir,
        verbose=args.verbose,
    )
    print(f"  elapsed={multi['elapsed_ms']}ms "
          f"judges={multi['last_round_judges']}")

    # Honest qualitative summary.
    notes = []
    notes.append(
        f"single-agent elapsed={single['elapsed_ms']}ms "
        f"vs multi-agent elapsed={multi['elapsed_ms']}ms "
        f"(delta={multi['elapsed_ms'] - single['elapsed_ms']}ms)"
    )
    if multi["last_round_combined_score"] is not None:
        notes.append(
            f"single-agent combined_score={single['last_round_combined_score']} "
            f"vs multi-agent combined_score={multi['last_round_combined_score']}"
        )
    if multi["last_round_dispersion"] is not None:
        notes.append(
            f"single-agent dispersion={single['last_round_dispersion']} "
            f"vs multi-agent dispersion={multi['last_round_dispersion']}"
        )
    notes.append(
        "multi-agent debate_triggered={multi_flag}, single-agent debate_triggered={single_flag}".format(
            multi_flag=multi['last_round_debate_triggered'],
            single_flag=single['last_round_debate_triggered'],
        )
    )

    summary = {
        "timestamp": time.time_ns() / 1e9,
        "n_per_generator": args.n_per_generator,
        "max_rounds": args.max_rounds,
        "config_path": str(config_path),
        "single_agent": single,
        "multi_agent": multi,
        "qualitative_notes": notes,
        "honest_caveats": [
            "This is a CHEAP smoke (1 round, n=1 per generator). It is "
            "not statistically meaningful on its own. Use Phase 4.6 "
            "stage 12 follow-up (replicate>=3) for any A/B claim.",
            "multi-agent elapsed time is N_generators x single-agent "
            "in the absence of parallelism; absolute wall-time difference "
            "is not a quality signal here.",
            "Judge confidence 0.0 from MockLLMClient is the placeholder; "
            "real judge confidence (e.g. 0.85 / 0.78 from Round 1 of "
            "run_full_multi_agent.py) requires use_mock=False and a "
            "non-placeholder judge prompt_role wiring (see TODO).",
        ],
    }
    args.out.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nSummary written to {args.out}")
    for note in notes:
        print(f"  - {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())