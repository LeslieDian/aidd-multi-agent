"""scripts/visualize_memory.py — P3-4 memory visualization.

Renders the 4-category RuleStore (negative / positive / context / evidence)
into PNG figures under ``docs/figures/`` so humans can see what the agent
has learned across runs.

Outputs (when matplotlib is available):
  memory_categories_pie.png      count distribution of the 4 categories
  calibration_scatter.png        predicted_delta vs observed_delta (EVIDENCE)
  calibration_drift.png           abs-error histogram + over/under balance
  top_rules_evidence.png          top-N rules ranked by evidence_strength
  calibration_cross_run.png       abs-error + under/over rate over time

Source of truth
---------------
The script reads every JSON file that matches one of:
  * ``runs/samples/rule_memory_<task_id>.json``
  * ``runs/<run_dir>/_memory/rule_memory.json``
  * ``memory/v2/<target>/<protocol>/<namespace>/rule_memory.json``

(these are the same paths the agent harness and the legacy loop use).

Usage
-----
::

    python scripts/visualize_memory.py                      # auto-discover all
    python scripts/visualize_memory.py --output docs/figures
    python scripts/visualize_memory.py --json-only          # skip PNGs
    python scripts/visualize_memory.py --cross-run          # per-file drift
    python scripts/visualize_memory.py --memory path/to/rules.json

The script NEVER writes back to the rule store. It is read-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Lazy matplotlib import so the JSON-only mode stays dependency-free.
HAS_MPL = True
try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    HAS_MPL = False


# ---------- discovery ------------------------------------------------------

DEFAULT_SEARCH_ROOTS = (Path("runs"), Path("memory"))


def discover_rule_memory_paths(roots: list[Path]) -> list[Path]:
    """Find every rule memory JSON file under the given search roots."""
    out: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("rule_memory*.json"):
            if path.is_file():
                out.append(path)
    # Stable, predictable order
    out.sort()
    return out


def load_rules(paths: list[Path]) -> list[dict]:
    """Concatenate rules from every rule memory JSON file."""
    rules: list[dict] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[WARN] could not read {path}: {exc}", file=sys.stderr)
            continue
        for rid, raw in (payload.get("rules") or {}).items():
            raw["_source_file"] = str(path)
            raw.setdefault("rule_id", rid)
            rules.append(raw)
    return rules


# ---------- aggregation ----------------------------------------------------


def category_counts(rules: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rules:
        cat = r.get("category", "?")
        counts[cat] = counts.get(cat, 0) + 1
    return counts


def evidence_pairs(rules: list[dict]) -> list[tuple[float, float, str, str, str]]:
    """Return (predicted, observed, parent_smiles, child_smiles, direction)
    for every EVIDENCE-rule observation. observations_log is expanded so
    accumulated pairs contribute every entry, not just the latest."""
    pairs: list[tuple[float, float, str, str, str]] = []
    for r in rules:
        if r.get("category") != "evidence_strength":
            continue
        log = r.get("pattern", {}).get("observations_log") or []
        parent = r.get("pattern", {}).get("parent_smiles", "?")
        child = r.get("pattern", {}).get("child_smiles", "?")
        direction = r.get("pattern", {}).get("direction", "?")
        if log:
            for entry in log:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    try:
                        pairs.append((float(entry[0]), float(entry[1]),
                                       parent, child, direction))
                    except (TypeError, ValueError):
                        continue
        else:
            # Backwards-compatible read for rules persisted before
            # observations_log existed.
            try:
                pairs.append((float(r["pattern"].get("predicted_delta", 0.0)),
                               float(r["pattern"].get("observed_delta", 0.0)),
                               parent, child, direction))
            except (TypeError, ValueError, KeyError):
                continue
    return pairs


def file_summaries(paths: list[Path]) -> list[dict]:
    """Per-file calibration summary so cross-run trends can be drawn.

    Each file is treated as a single point in time. Returns rows ordered
    by the embedded ISO timestamp (saved_utc -> last_updated -> file mtime),
    so even old runs without an explicit timestamp sort sensibly.

    Schema:
      {path, label, n_pairs, mean_abs_error, max_abs_error,
       under_claim_rate, over_claim_rate, sort_key}

    `label` is the file's basename (sans extension) — short enough to fit
    on the x-axis of a time-series plot.
    """
    rows: list[dict] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rules = list((payload.get("rules") or {}).values())
        pairs = evidence_pairs(rules)
        if not pairs:
            # Skip files with zero EVIDENCE observations: they cannot
            # contribute a meaningful drift point and would just stretch
            # the x-axis with empty bins.
            continue
        errors = [abs(p - o) for p, o, *_ in pairs]
        n_under = sum(1 for p, o, *_ in pairs if p > o)
        n = len(pairs)
        # Sort key: prefer saved_utc / last_updated, fall back to file mtime.
        sort_key = (payload.get("saved_utc")
                     or max(
                         (r.get("last_updated_utc") or r.get("created_utc") or "")
                         for r in rules
                     )
                     or "")
        if not sort_key:
            try:
                sort_key = path.stat().st_mtime_iso  # type: ignore[attr-defined]
            except Exception:
                sort_key = str(path.stat().st_mtime)
        rows.append({
            "path": str(path),
            "label": path.stem,
            "n_pairs": n,
            "n_rules": len([r for r in rules if r.get("category") == "evidence_strength"]),
            "mean_abs_error": round(sum(errors) / n, 4),
            "max_abs_error": round(max(errors), 4),
            "under_claim_rate": round(n_under / n, 3),
            "over_claim_rate": round((n - n_under) / n, 3),
            "sort_key": sort_key,
        })
    rows.sort(key=lambda r: r["sort_key"])
    return rows


# ---------- figures --------------------------------------------------------


def plot_category_pie(counts: dict, out_dir: Path) -> Path | None:
    if not HAS_MPL:
        return None
    if not counts:
        return None
    fig, ax = plt.subplots(figsize=(6, 6))
    labels = list(counts.keys())
    values = list(counts.values())
    colors = {
        "negative_constraint": "#d62728",
        "positive_transformation": "#2ca02c",
        "applicable_context": "#9467bd",
        "evidence_strength": "#1f77b4",
    }
    color_list = [colors.get(label, "#7f7f7f") for label in labels]
    wedges, texts, autotexts = ax.pie(
        values, labels=labels, autopct="%1.0f%%",
        colors=color_list, startangle=90,
        wedgeprops={"edgecolor": "white", "linewidth": 1.5},
        textprops={"fontsize": 10},
    )
    ax.set_title(f"4-category memory: {sum(values)} rules total", fontsize=12)
    fig.tight_layout()
    path = out_dir / "memory_categories_pie.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_calibration_scatter(pairs, out_dir: Path) -> Path | None:
    if not HAS_MPL or not pairs:
        return None
    fig, ax = plt.subplots(figsize=(7, 6))
    under = [(p, o) for p, o, *_ in pairs if p > o]
    over = [(p, o) for p, o, *_ in pairs if p <= o]
    if under:
        ax.scatter([p for p, _ in under], [o for _, o in under],
                    s=42, alpha=0.7, color="#d62728",
                    label=f"under-claim (n={len(under)})")
    if over:
        ax.scatter([p for p, _ in over], [o for _, o in over],
                    s=42, alpha=0.7, color="#2ca02c",
                    label=f"over-claim (n={len(over)})")
    # y=x reference line: perfect calibration.
    all_vals = [v for pair in pairs for v in pair[:2]]
    if all_vals:
        lo, hi = min(all_vals), max(all_vals)
        margin = max(abs(lo), abs(hi)) * 0.1 + 0.005
        ax.plot([lo - margin, hi + margin], [lo - margin, hi + margin],
                 "--", color="#7f7f7f", linewidth=1, label="y = x (perfect)")
    ax.set_xlabel("predicted Δ property_score")
    ax.set_ylabel("observed Δ property_score")
    ax.set_title("Calibration: predicted vs observed (EVIDENCE rules)")
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    path = out_dir / "calibration_scatter.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_calibration_drift(pairs, out_dir: Path) -> Path | None:
    if not HAS_MPL or not pairs:
        return None
    errors = [abs(p - o) for p, o, *_ in pairs]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    axes[0].hist(errors, bins=min(20, max(3, len(errors))),
                  color="#1f77b4", edgecolor="white", alpha=0.85)
    axes[0].set_xlabel("|predicted − observed|")
    axes[0].set_ylabel("observations")
    axes[0].set_title(f"abs error histogram (n={len(errors)}, "
                       f"mean={sum(errors) / len(errors):.4f})")
    axes[0].grid(alpha=0.25)
    n_under = sum(1 for p, o, *_ in pairs if p > o)
    n_over = sum(1 for p, o, *_ in pairs if p <= o)
    axes[1].bar(["under-claim\n(predicted > observed)", "over-claim\n(predicted ≤ observed)"],
                 [n_under, n_over], color=["#d62728", "#2ca02c"], alpha=0.85,
                 edgecolor="white")
    axes[1].set_ylabel("observations")
    axes[1].set_title("Calibration direction")
    axes[1].grid(alpha=0.25, axis="y")
    for ax in axes:
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
    fig.tight_layout()
    path = out_dir / "calibration_drift.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_top_rules(rules: list[dict], out_dir: Path,
                    top_n: int = 15) -> Path | None:
    if not HAS_MPL or not rules:
        return None
    # Sort by evidence_strength, then observations (more observations first
    # if strength is tied).
    sorted_rules = sorted(
        rules,
        key=lambda r: (float(r.get("evidence_strength") or 0.0),
                          int(r.get("observations") or 0)),
        reverse=True,
    )[:top_n]
    if not sorted_rules:
        return None
    labels = []
    strengths = []
    colors = []
    cmap = {
        "negative_constraint": "#d62728",
        "positive_transformation": "#2ca02c",
        "applicable_context": "#9467bd",
        "evidence_strength": "#1f77b4",
    }
    for r in sorted_rules:
        rid = r.get("rule_id", "?")
        cat = r.get("category", "?")
        short = (rid[:40] + "…") if len(rid) > 40 else rid
        labels.append(f"[{cat}] {short}")
        strengths.append(float(r.get("evidence_strength") or 0.0))
        colors.append(cmap.get(cat, "#7f7f7f"))
    fig, ax = plt.subplots(figsize=(9, max(4, 0.32 * len(labels) + 1)))
    y_pos = list(range(len(labels)))
    ax.barh(y_pos, strengths, color=colors, alpha=0.85, edgecolor="white")
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=8, family="monospace")
    ax.invert_yaxis()
    ax.set_xlabel("evidence_strength")
    ax.set_title(f"Top {len(labels)} rules by evidence_strength")
    ax.set_xlim(0, max(1.0, max(strengths) * 1.05))
    ax.grid(alpha=0.25, axis="x")
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()
    path = out_dir / "top_rules_evidence.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_calibration_cross_run(file_rows: list[dict],
                                 out_dir: Path) -> Path | None:
    """Calibration trend across multiple rule memory files.

    Each file becomes one x-axis point (sorted by saved_utc / last_updated
    / file mtime). The figure shows two y-axes:
      * mean |predicted - observed| (left) — should drop if the agent
        is learning to calibrate
      * under / over claim rates (right) — should approach 0.5 / 0.5
        if the agent is unbiased
    A descending line of mean_abs_error is the "agent is learning to
    calibrate" signal; a flat or rising line is "no calibration learning,
    just noise".
    """
    if not HAS_MPL or len(file_rows) < 1:
        return None
    fig, ax_left = plt.subplots(figsize=(9, 5))
    x = list(range(len(file_rows)))
    labels = [r["label"] for r in file_rows]
    mean_err = [r["mean_abs_error"] for r in file_rows]
    ax_left.plot(x, mean_err, "o-", color="#1f77b4", linewidth=2, markersize=8,
                  label="mean |predicted − observed|")
    ax_left.fill_between(x, 0, mean_err, color="#1f77b4", alpha=0.1)
    ax_left.set_ylabel("mean abs error (property Δ)", color="#1f77b4")
    ax_left.set_ylim(bottom=0)
    ax_left.tick_params(axis="y", labelcolor="#1f77b4")
    ax_left.grid(alpha=0.25)
    ax_right = ax_left.twinx()
    under = [r["under_claim_rate"] for r in file_rows]
    over = [r["over_claim_rate"] for r in file_rows]
    ax_right.plot(x, under, "s--", color="#d62728", linewidth=1.5,
                   markersize=6, label="under-claim rate")
    ax_right.plot(x, over, "^--", color="#2ca02c", linewidth=1.5,
                   markersize=6, label="over-claim rate")
    ax_right.axhline(0.5, color="#7f7f7f", linestyle=":", linewidth=1,
                       alpha=0.7)
    ax_right.set_ylabel("rate (0–1)", color="#7f7f7f")
    ax_right.set_ylim(0, 1)
    ax_left.set_xticks(x)
    ax_left.set_xticklabels(
        [l if len(l) <= 30 else l[:27] + "…" for l in labels],
        rotation=30, ha="right", fontsize=8, family="monospace",
    )
    ax_left.set_xlabel("rule_memory file (sorted by saved_utc / mtime)")
    ax_left.set_title(
        f"Calibration drift across {len(file_rows)} rule memory file(s)"
    )
    # Combined legend (left + right)
    lines1, labels1 = ax_left.get_legend_handles_labels()
    lines2, labels2 = ax_right.get_legend_handles_labels()
    ax_left.legend(lines1 + lines2, labels1 + labels2,
                    loc="upper right", fontsize=9)
    fig.tight_layout()
    path = out_dir / "calibration_cross_run.png"
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ---------- JSON report ----------------------------------------------------


def write_json_report(rules, pairs, counts, out_dir: Path,
                      file_rows: list[dict] | None = None) -> Path:
    errors = [abs(p - o) for p, o, *_ in pairs]
    n_under = sum(1 for p, o, *_ in pairs if p > o)
    n_over = len(pairs) - n_under
    report = {
        "schema_version": 2,
        "n_rules_total": len(rules),
        "n_evidence_observations": len(pairs),
        "category_counts": counts,
        "calibration": {
            "mean_abs_error": round(sum(errors) / len(errors), 4) if errors else None,
            "max_abs_error": round(max(errors), 4) if errors else None,
            "under_claim_count": n_under,
            "over_claim_count": n_over,
            "under_claim_rate": round(n_under / len(pairs), 3) if pairs else None,
        },
        "top_rules": sorted(
            [
                {
                    "rule_id": r.get("rule_id"),
                    "category": r.get("category"),
                    "evidence_strength": float(r.get("evidence_strength") or 0.0),
                    "observations": int(r.get("observations") or 0),
                    "source_file": r.get("_source_file"),
                }
                for r in rules
            ],
            key=lambda r: (r["evidence_strength"], r["observations"]),
            reverse=True,
        )[:20],
        "cross_run": file_rows or [],
    }
    path = out_dir / "memory_visualization.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                     encoding="utf-8")
    return path


# ---------- main -----------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/figures",
                         help="Directory to write PNG / JSON outputs")
    parser.add_argument("--json-only", action="store_true",
                         help="Skip matplotlib figures (no PNGs)")
    parser.add_argument("--cross-run", action="store_true",
                         help="Emit calibration_cross_run.png with per-file "
                              "drift (mean abs error + over/under rates over "
                              "time). Always include the cross_run block in "
                              "memory_visualization.json, even without this "
                              "flag.")
    parser.add_argument("--memory", action="append", default=[],
                         help="Explicit rule_memory JSON paths (repeatable). "
                              "If not given, the script auto-discovers files "
                              "under ./runs and ./memory.")
    parser.add_argument("--top-n", type=int, default=15,
                         help="How many rules to show in the top-rules chart")
    args = parser.parse_args(argv)

    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.memory:
        paths = [Path(p) for p in args.memory]
    else:
        paths = discover_rule_memory_paths(list(DEFAULT_SEARCH_ROOTS))

    if not paths:
        print("[INFO] no rule memory files found; "
              "run an agent task or a loop first.", file=sys.stderr)
        return 1

    rules = load_rules(paths)
    counts = category_counts(rules)
    pairs = evidence_pairs(rules)
    file_rows = file_summaries(paths)
    print(f"[viz] {len(rules)} rules from {len(paths)} files "
          f"(EVIDENCE observations: {len(pairs)}, "
          f"files with EVIDENCE data: {len(file_rows)})")

    report_path = write_json_report(rules, pairs, counts, out_dir,
                                      file_rows=file_rows)
    print(f"[viz] wrote {report_path}")

    if not args.json_only:
        if not HAS_MPL:
            print("[WARN] matplotlib not installed; skipping PNGs. "
                  "Install with `pip install matplotlib` to enable figures.",
                  file=sys.stderr)
            return 0
        outputs = [
            plot_category_pie(counts, out_dir),
            plot_calibration_scatter(pairs, out_dir),
            plot_calibration_drift(pairs, out_dir),
            plot_top_rules(rules, out_dir, top_n=args.top_n),
        ]
        if args.cross_run or len(file_rows) >= 2:
            # Cross-run plot is only useful with >=2 files; if the user
            # explicitly asked for it, write the figure even with one file
            # (a single point is degenerate but the JSON still captures it).
            path = plot_calibration_cross_run(file_rows, out_dir)
            if path:
                outputs.append(path)
        for path in outputs:
            if path:
                print(f"[viz] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())