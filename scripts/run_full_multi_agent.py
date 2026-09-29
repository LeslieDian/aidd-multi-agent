"""scripts/run_full_multi_agent.py - End-to-end multi-agent loop smoke.

Runs run_multi_agent_loop() with max_rounds=2, n_per_generator=2,
against real providers (MiniMax + Qwen verified live 2026-09-29;
GLM and Kimi kept out because glm-5.3-flash burns all tokens on
reasoning and returns empty content). Validates the FULL per-round
pipeline:

    router -> PARENTS -> per-gen focus -> generators -> aggregate
    -> evaluate -> multi-judge -> debate -> next round focus.

Writes a JSON summary to runs/samples/multi_agent_full_<timestamp>.json.

This is the cheapest meaningful end-to-end verification of the
multi-agent design (no n>=5 repetition; no real A/B; just 'does the
loop close on real LLM calls'). For the latter, see Phase 4.6 stage 9.
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
    parser = argparse.ArgumentParser(description="Full multi-agent loop smoke.")
    parser.add_argument("--n-per-generator", type=int, default=2)
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--out", default=None,
                        help="JSON summary path. Default: "
                             "runs/samples/multi_agent_full_<ts>.json")
    parser.add_argument("--mock", action="store_true",
                        help="Use mock providers (no LLM call).")
    args = parser.parse_args()

    import yaml
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    from loop_multi_agent import (
        run_multi_agent_loop,
        multi_agent_enabled,
    )

    if not multi_agent_enabled(config.get("loop") or {}):
        print("[multi_agent] loop.multi_agent.enabled=false in config.yaml; aborting.")
        return 1

    print(f"Multi-agent full loop: "
          f"n_per_generator={args.n_per_generator} "
          f"max_rounds={args.max_rounds} "
          f"mock={args.mock}")
    started = time.time_ns()
    result = run_multi_agent_loop(
        config=config,
        output_dir="runs/_multi_agent_full",
        n_per_generator=args.n_per_generator,
        max_rounds=args.max_rounds,
        use_mock=args.mock,
        verbose=True,
    )
    elapsed_ms = round((time.time_ns() - started) / 1e6, 1)
    print(f"\nTotal wall time: {elapsed_ms} ms")

    out = Path(args.out) if args.out else (
        ROOT / "runs" / "samples"
        / f"multi_agent_full_{time.strftime('%Y%m%d_%H%M%S')}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "timestamp": time.time_ns() / 1e9,
        "elapsed_ms": elapsed_ms,
        "n_per_generator": args.n_per_generator,
        "max_rounds": args.max_rounds,
        "mock": args.mock,
        "n_generators": result["n_generators"],
        "n_judges": result["n_judges"],
        "rounds_log": result["rounds_log"],
        "warnings": result.get("warnings", []),
        "note": result.get("note", ""),
    }
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    print(f"\nSummary written to {out}")
    print(f"Rounds logged: {len(result['rounds_log'])}")
    for i, r in enumerate(result["rounds_log"]):
        print(f"  Round {i}: expert={r.get('expert_prompt')!r} "
              f"cands={r.get('n_candidates')} enriched={r.get('n_enriched')} "
              f"judges={[(j['name'], j['score']) for j in r.get('judges', [])]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())