"""Diagnose why the reflection group regressed in real_ablation_v3.

For each eligible reflection run, prints the judge's focus/reflection/adopted
and the round-by-round Vina trajectory side by side.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def diagnose(group_dir: Path) -> None:
    if not group_dir.exists():
        print(f"(missing) {group_dir}")
        return

    for repeat_dir in sorted(group_dir.iterdir()):
        if not repeat_dir.is_dir():
            continue
        attempt_dirs = sorted(repeat_dir.glob("attempt_*"))
        if not attempt_dirs:
            continue
        attempt = attempt_dirs[-1]  # latest attempt
        summary = attempt / "summary.json"
        if not summary.exists():
            continue
        meta = json.loads(summary.read_text(encoding="utf-8"))
        if meta.get("status") != "finished":
            continue

        rounds = sorted(attempt.glob("round_*.json"))
        print(f"\n=== {repeat_dir.parent.name}/{repeat_dir.name} "
              f"({len(rounds)} rounds, status={meta['status']}) ===")
        prev_best_vina: float | None = None
        for r in rounds:
            data = json.loads(r.read_text(encoding="utf-8"))
            candidates = data.get("candidates", [])
            complete = [
                c for c in candidates
                if c.get("evaluation_status") == "complete"
                and c.get("dock", {}).get("score") is not None
            ]
            vinas = sorted(c["dock"]["score"] for c in complete)
            round_best = vinas[0] if vinas else None
            delta = (round_best - prev_best_vina) if (round_best is not None and prev_best_vina is not None) else None
            prev_best_vina = round_best
            j = data.get("judgment") or {}
            print(
                f"  round {r.stem.split('_')[-1]}: "
                f"best_vina={round_best!s:>7s}  "
                f"delta_vs_prev={delta!s:>7s}  "
                f"complete={len(complete)}/{len(candidates)}  "
                f"conf={j.get('confidence')!s:>6s}  "
                f"adopted={j.get('adopted_count')!s:>4s}  "
                f"focus={(j.get('focus') or '?')[:60]!r}"
            )
            reflection = (j.get("reflection") or "").strip()
            if reflection:
                print(f"      reflection: {reflection[:160]!r}")


def main() -> int:
    base = ROOT / "benchmarks/real_ablation_v3_20260915"
    print("--- BASELINE ---")
    diagnose(base / "baseline")
    print("\n\n--- REFLECTION ---")
    diagnose(base / "reflection")
    print("\n\n--- REFLECTION_MEMORY ---")
    diagnose(base / "reflection_memory")
    return 0


if __name__ == "__main__":
    sys.exit(main())