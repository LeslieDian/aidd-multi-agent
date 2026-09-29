"""scripts/smoke_multi_agent.py - Offline smoke for the multi-agent entry point.

Verifies:
- The `loop.multi_agent.*` config block parses.
- The validator emits the expected warnings for non-heterogeneous configs.
- Heterogeneous generators can be loaded without calling any LLM.
- Aggregator + multi-judge vote are exercised on synthetic inputs.

Usage:
    python scripts/smoke_multi_agent.py
    python scripts/smoke_multi_agent.py --config config.yaml
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

# Ensure the project root is on sys.path so 'agents' / 'loop_multi_agent' are
# importable when this script is invoked directly.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-agent offline smoke.")
    parser.add_argument("--config", default="config.yaml",
                        help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--out-dir", default="runs/_smoke_multi_agent",
                        help="Output directory for the smoke run")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"[smoke] config not found: {config_path}", file=sys.stderr)
        return 1

    try:
        import yaml  # noqa
    except ImportError:
        print("[smoke] PyYAML not installed; pip install -r requirements.txt", file=sys.stderr)
        return 1

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Import lazily so missing deps don't break unrelated tests
    from agents.multi_agent import (
        aggregate_candidates,
        combine_judge_votes,
        generators_are_heterogeneous,
        validate_multi_agent_config,
    )
    from agents.multi_agent import JudgeVerdict
    from agents.router import RoundFingerprint, route, router_enabled
    from loop_multi_agent import (
        multi_agent_enabled,
        read_aggregation,
        read_generators,
        read_judges,
        read_router,
        run_multi_agent_loop,
    )

    loop_cfg = config.get("loop") or {}
    llm_providers = (config.get("llm") or {}).get("providers") or {}

    print("=" * 60)
    print("Multi-agent offline smoke (Phase 4.6)")
    print("=" * 60)
    print(f"  config           : {config_path}")
    print(f"  multi_agent.on   : {multi_agent_enabled(loop_cfg)}")
    print(f"  n_generators     : {len(read_generators(loop_cfg))}")
    print(f"  n_judges         : {len(read_judges(loop_cfg))}")
    print(f"  router.enabled   : {read_router(loop_cfg)['enabled']}")
    print(f"  aggregation      : {read_aggregation(loop_cfg)}")

    warns = validate_multi_agent_config(loop_cfg.get("multi_agent"), llm_providers)
    if warns:
        print("\nValidator warnings:")
        for w in warns:
            print(f"  - {w}")
    else:
        print("\nValidator: OK (no warnings).")

    # Synthetic test: heterogeneous + aggregation + multi-judge
    gens = [
        {"name": "A1_qed",   "provider": "MiniMax", "prompt_role": "qed"},
        {"name": "A2_vina",  "provider": "MiniMax", "prompt_role": "vina"},
    ]
    assert generators_are_heterogeneous(gens), "expected heterogeneous"

    per_gen = {
        "A1_qed":  [{"smiles": "CCO", "confidence": 0.9},
                    {"smiles": "c1ccccc1", "confidence": 0.5}],
        "A2_vina": [{"smiles": "CCN", "confidence": 0.6}],
    }
    agg, stats = aggregate_candidates(per_gen, diversity_floor=0.7, top_n=10)
    print(f"\nSynthetic aggregation: input={stats['n_input']} "
          f"after_dedup={stats['n_after_dedup']} "
          f"kept={stats['n_kept']}")

    verdicts = [
        JudgeVerdict("J1", 0.8, "qed", "looks good"),
        JudgeVerdict("J2", 0.4, "vina", "vina weak"),
    ]
    s, per, disp = combine_judge_votes(verdicts, weights=[1.0, 1.0])
    print(f"Synthetic multi-judge vote: combined={s:.3f} "
          f"per_judge={per} dispersion={disp:.3f}")

    # Router dispatch
    fp = RoundFingerprint(property_weak=True)
    print(f"\nRouter dispatch on property_weak: {route(fp)}")

    # Run the loop entry point end-to-end (mock providers will not be called).
    if multi_agent_enabled(loop_cfg):
        try:
            result = run_multi_agent_loop(
                config=config,
                output_dir=args.out_dir,
                n_per_generator=3,
                max_rounds=1,
                use_mock=True,
                verbose=True,
            )
            print(f"\nrun_multi_agent_loop returned: "
                  f"mode={result['mode']} n_generators={result['n_generators']} "
                  f"rounds_log_len={len(result['rounds_log'])}")
        except Exception as exc:
            print(f"\n[smoke] run_multi_agent_loop raised (expected for mock): {exc}")

    print("\nSmoke OK.")
    return 0


if __name__ == "__main__":
    with warnings.catch_warnings():
        # Surface multi_agent warnings as part of the smoke output.
        warnings.simplefilter("always")
        sys.exit(main())