"""Collect r2 rows for the three parents and print a side-by-side view of
pre-fix, fix-verification, and r2 for each parent."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOUT = json.loads((ROOT / "runs/samples/parent_scout_20260920.json").read_text(encoding="utf-8"))
picks = SCOUT["usable_parents"][:3]
R2 = "r2_20260921"


def read_arms(tag):
    rows = []
    for p in picks:
        slug = p["name"].replace(" ", "_")
        out = ROOT / "runs" / f"diagnostic_2d_{tag}_{slug}"
        if not (out / "comparison.json").exists():
            continue
        comparison = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
        for arm in ("rule", "agent"):
            m = comparison["arms"][arm]
            rows.append({"parent": p["name"], "arm": arm,
                         "termination_outcome": m["termination_outcome"],
                         "new_structure_evaluations": m["new_structure_evaluations"],
                         "qualifying": m["successful_screened_products"],
                         "best_delta": m["best_compliant_delta"],
                         "network_failures": m["network_failures"],
                         "network_retries": m["network_retries"],
                         "stop_evidence_valid": m["stop_evidence_valid"]})
    return rows


r2 = read_arms(R2)
(ROOT / "runs/samples" / f"diagnostic_2d_{R2}_rows.json").write_text(
    json.dumps(r2, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote", len(r2), "r2 rows")
for r in r2:
    print(f"{r['parent']:8} {r['arm']:5} {r['termination_outcome']:26} "
          f"qual={r['qualifying']} best={r['best_delta']} net_fail={r['network_failures']}")

print("\n===== combined view (pre-fix / fix / r2) agent arm only")
print(f"{'parent':8} {'pre_fix':35} {'fix':35} {'r2':35}")
pre = {r["parent"]: r for r in json.loads((ROOT / "runs/samples/diagnostic_2d_stability_20260920_rows.json").read_text(encoding="utf-8")) if r["arm"] == "agent"}
fix = {r["parent"]: r for r in json.loads((ROOT / "runs/samples/diagnostic_2d_fixed_20260920_rows.json").read_text(encoding="utf-8")) if r["arm"] == "agent"}
r2a = {r["parent"]: r for r in r2 if r["arm"] == "agent"}
for p in picks:
    print(f"{p['name']:8} {str(pre[p['name']]['termination_outcome']):12} q={pre[p['name']]['qualifying']} "
          f"{str(fix[p['name']]['termination_outcome']):12} q={fix[p['name']]['qualifying']} "
          f"{str(r2a[p['name']]['termination_outcome']):12} q={r2a[p['name']]['qualifying']}")
