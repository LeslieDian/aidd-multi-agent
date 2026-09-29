"""scripts/run_multi_agent_round.py - Real multi-agent single-round smoke.

Calls each generator (MiniMax + GLM + Qwen, verified live 2026-09-29)
once with n_per_generator=2, aggregates, and exits. Validates that the
multi-agent entry point can drive cross-provider real calls.

This is NOT a full multi-agent run (no evaluator, no judge vote, no
PARENTS block). It just proves the generators dispatch correctly.
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Real multi-agent 1 round.")
    parser.add_argument("--n-per-generator", type=int, default=2,
                        help="Candidates requested from each generator.")
    parser.add_argument("--out", default="runs/samples/multi_agent_round_20260929.json")
    args = parser.parse_args()

    import yaml
    config_path = ROOT / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    from loop_multi_agent import (
        call_multi_generators,
        aggregate_round,
        read_aggregation,
        read_generators,
        multi_agent_enabled,
    )
    from tools.mutate import format_parents_block

    loop_cfg = config.get("loop") or {}
    if not multi_agent_enabled(loop_cfg):
        print("[multi_agent] loop.multi_agent.enabled=false; aborting.")
        return 1

    gens = read_generators(loop_cfg)
    print(f"Multi-agent round: {len(gens)} generators, n_per_generator={args.n_per_generator}")

    # Empty parents for this smoke (round 0 has no prior history).
    parents_block = ""

    started = time.time_ns()
    per_gen = call_multi_generators(
        config=config,
        generators=gens,
        n_per_generator=args.n_per_generator,
        focus="",
        weakness="",
        memory_context="",
        failed_prompt="",
        parents_block=parents_block,
        use_mock=False,
    )
    elapsed_ms = round((time.time_ns() - started) / 1e6, 1)
    print(f"Total generator call time: {elapsed_ms} ms")

    print()
    print(f"{'GENERATOR':<25} {'SMILES COUNT':<15} EXAMPLES")
    for name, items in per_gen.items():
        examples = [c["smiles"] for c in items[:3]]
        print(f"  {name:<23} {len(items):<15} {examples}")

    agg, stats = aggregate_round(
        per_gen,
        aggregation_cfg=read_aggregation(loop_cfg),
    )
    print()
    print(f"Aggregator: input={stats['n_input']} "
          f"after_dedup={stats['n_after_dedup']} "
          f"dropped_diversity={stats['n_dropped_diversity']} "
          f"kept={stats['n_kept']}")

    # Persist output (no api keys!).
    summary = {
        "timestamp": time.time_ns() / 1e9,
        "elapsed_ms": elapsed_ms,
        "n_generators": len(gens),
        "n_per_generator": args.n_per_generator,
        "per_generator": {
            name: [c["smiles"] for c in items]
            for name, items in per_gen.items()
        },
        "aggregation_stats": stats,
        "aggregated_smiles": [c.smiles for c in agg],
        "aggregated_sources": [list(c.sources) for c in agg],
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nSummary written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())