"""Show per-run Vina progression across rounds, plus completion stats."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _per_run_vina_progression(benchmark_dir: Path, group: str, repeat: int = 1) -> list[float] | None:
    paths = sorted((benchmark_dir / group / f"repeat_{repeat:02d}" / "attempt_01").glob("round_*.json"))
    if not paths:
        return None
    out = []
    for r in paths:
        rd = json.loads(r.read_text(encoding="utf-8"))
        vinas = [
            c.get("dock", {}).get("score")
            for c in rd.get("candidates", [])
            if c.get("dock", {}).get("score") is not None and math.isfinite(c["dock"]["score"])
        ]
        if vinas:
            out.append(min(vinas))
    return out


def _benchmark_stats(d: Path) -> dict | None:
    mf = d / "benchmark_manifest.json"
    if not mf.exists():
        return None
    m = json.loads(mf.read_text(encoding="utf-8"))
    elig = sum(1 for r in m.get("runs", []) if r.get("eligible"))
    total = len(m.get("runs", []))
    elapsed = sum((r.get("duration_seconds") or 0) for r in m.get("runs", []))
    return {
        "started": (m.get("started_at") or "?")[:16],
        "finished": (m.get("finished_at") or "?")[:16] if m.get("finished_at") else "?",
        "eligible": elig,
        "total": total,
        "duration_min": round(elapsed / 60, 1),
        "matrix": m.get("name"),
        "repeats": m["execution"].get("repeats"),
    }


def main() -> int:
    print("== Benchmark completion ==")
    for d in sorted(ROOT.glob("benchmarks/*")):
        if not d.is_dir():
            continue
        if "real_ablation" not in d.name and "confirmatory" not in d.name:
            continue
        s = _benchmark_stats(d)
        if not s:
            continue
        print(f"{d.name:40s}  {s['started']} -> {s['finished']}  "
              f"eligible={s['eligible']}/{s['total']}  total={s['duration_min']:.1f}min  "
              f"repeats={s['repeats']}")

    print()
    print("== Per-run Vina progression (best Vina each round, first eligible run) ==")
    for d in sorted(ROOT.glob("benchmarks/*")):
        if not d.is_dir() or "real_ablation" not in d.name:
            continue
        for group in ("baseline", "reflection", "reflection_memory"):
            for repeat in (1, 2, 3):
                v = _per_run_vina_progression(d, group, repeat)
                if v:
                    print(f"{d.name:30s} {group:20s} r{repeat}: "
                          f"{[round(x, 2) for x in v]}  "
                          f"delta={round(v[-1] - v[0], 2) if len(v) > 1 else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())