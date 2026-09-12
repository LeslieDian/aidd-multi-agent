"""tests/test_loop.py - End-to-end test of the loop in mock mode.

Run: python tests/test_loop.py

Expected: 2 rounds, ~10 candidates each, all 4 tools exercised, runs/*.json written.
"""
from __future__ import annotations

import json
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loop import run_loop, load_config


def main():
    cfg = load_config("config.yaml")

    # Clean up old test runs to assert fresh output
    test_runs = Path("runs_test")
    if test_runs.exists():
        shutil.rmtree(test_runs)

    print("[TEST] running 2-round loop in mock mode...")
    overall = run_loop(
        config=cfg,
        output_dir="runs_test",
        max_rounds=2,
        n_per_provider=3,
        dock_enabled=True,
        use_mock=True,
        verbose=True,
    )

    # Verify outputs
    files = sorted(Path("runs_test").glob("*.json"))
    assert files, "no output files written"
    print(f"\n[TEST] output files: {[f.name for f in files]}")

    # Check round_0.json structure
    r0 = json.loads((test_runs / "round_0.json").read_text(encoding="utf-8"))
    assert "candidates" in r0
    assert "summary" in r0
    assert "judgment" in r0
    assert r0["summary"]["n_total"] > 0
    print(f"  [OK] round 0: {r0['summary']['n_total']} candidates, "
          f"{r0['summary']['n_valid']} valid, "
          f"{r0['summary']['n_unique_scaffolds']} unique scaffolds")
    print(f"  [OK] focus from judge: {r0['focus'][:80]}")

    # Check that dock ran
    dock_results = [c["dock"] for c in r0["candidates"] if c["validate"]["valid"]]
    n_with_score = sum(1 for d in dock_results if d.get("score") is not None)
    print(f"  [OK] {n_with_score}/{len(dock_results)} valid molecules have a Vina score")

    # Check summary.json
    summary = json.loads((test_runs / "summary.json").read_text(encoding="utf-8"))
    assert summary["rounds_completed"] == 2
    print(f"  [OK] summary: {summary['rounds_completed']} rounds")

    # Clean up
    shutil.rmtree(test_runs)
    print("\n[OK] End-to-end loop test passed")


if __name__ == "__main__":
    main()