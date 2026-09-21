"""Second repetition of the fixed-version stability study on the same three
parents, on the same frozen version. Each parent gets exactly one new real
agent arm (and one new real rule arm); no arm is re-run, and no result is
selected for being better. Token plan was exhausted earlier this morning and
recovered since then (see 2026-09-21_v2 connectivity gate)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOUT = ROOT / "runs/samples/parent_scout_20260920.json"
TAG = "r2_20260921"

scout = json.loads(SCOUT.read_text(encoding="utf-8"))
picks = scout["usable_parents"][:3]


def stage(name, out, *extra):
    cmd = [sys.executable, "scripts/compare_2d_policies.py", name, "--output", str(out), *extra]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(f"!! {name} FAILED\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
        raise SystemExit(1)
    return result.stdout


for p in picks:
    slug = p["name"].replace(" ", "_")
    out = ROOT / "runs" / f"diagnostic_2d_{TAG}_{slug}"
    print(f"\n===== {p['name']} ({p['parent_smiles']}) -> {out.name}")
    stage("prepare", out, "--scenario", "parent", "--parent", p["parent_smiles"])
    stage("audit", out)
    stage("rule", out)
    stage("agent", out)
    stage("report", out)

print("\n===== all 6 arms complete")
