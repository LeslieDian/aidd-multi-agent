"""Control-enrichment validation for the current docking protocol (no LLM involved).

Answers one question: under the *current* receptor / pocket / Vina settings, does
docking actually separate known EGFR actives from property-matched decoys?

Panel (default):
- actives  : deterministic sample of DUD-E EGFR actives
- decoys   : deterministic sample of DUD-E EGFR property-matched decoys
- anchors  : erlotinib / gefitinib / afatinib from tools.references, reported
             separately so they are not double-counted in the enrichment stats

Outputs inside --output:
- report.json / report.md : metrics, sampling list, external-data provenance
- scores.csv              : per-molecule rows (group, id, descriptors, score)
- vina_vs_heavy_atoms.png : size-bias diagnostic (only if matplotlib is present)

Usage:
  python scripts/validate_controls.py --output runs/controls_validation_20260917
  python scripts/validate_controls.py --output /tmp/controls_smoke \
      --n-actives 3 --n-decoys 6 --exhaustiveness 4 --workers 2
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
from rdkit import Chem, RDLogger
from rdkit.Chem import Descriptors

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop import load_config
from tools.dock_score import dock_smiles, resolve_vina_binary, validate_receptor
from tools.provenance import digest, docking_protocol, file_hash
from tools.references import load_references

RDLogger.DisableLog("rdApp.*")

DUDE_BASE = "https://dude.docking.org/targets/egfr"
DUDE_SOURCES = {
    "actives": f"{DUDE_BASE}/actives_final.ism",
    "decoys": f"{DUDE_BASE}/decoys_final.ism",
}

# Repo-relative cache for external control data; data/raw/ is already git-ignored.
DEFAULT_CACHE_DIR = "data/raw/egfr_controls"


# ---------------------------------------------------------------- external data

def _download(url: str, session: requests.Session, attempts: int = 3) -> bytes:
    """The DUD-E host is an old Apache that occasionally drops a connection mid-handshake."""
    last = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=120)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            last = exc
            print(f"  download attempt {attempt}/{attempts} failed: {type(exc).__name__}", flush=True)
            time.sleep(3 * attempt)
    raise RuntimeError(f"Could not download {url}: {last}")


def ensure_dude_file(name: str, url: str, cache_dir: Path, session: requests.Session,
                     attempts: int = 4, min_rows: int = 500, local_file: str | None = None) -> dict:
    """Fetch a DUD-E panel file into cache_dir and return auditable provenance.

    The DUD-E host is an ancient Apache that drops long connections without a
    Content-Length header and without Range support, so a partial file cannot be
    resumed and cannot be detected by size. Strategy: take several attempts, keep
    the largest file that parses, and record whether it ended on a line boundary.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"dude_egfr_{name}.ism"
    toml = path.with_suffix(".provenance.json")

    if local_file:
        source_bytes = Path(local_file).read_bytes()
        if source_bytes.lstrip()[:1] == b"<":
            raise ValueError(f"{local_file} looks like an HTML error page, not a SMILES file")
        path.write_bytes(source_bytes)
        rows = read_smi(path)
        record = {"url": None, "origin": f"local file {Path(local_file).resolve()}",
                  "sha256": file_hash(path), "bytes": len(source_bytes), "rows": len(rows),
                  "trailing_newline": source_bytes.endswith(b"\n"),
                  "attempts": [], "partial_download": False,
                  "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "local_path": str(path.resolve()), "reused_from_cache": False}
        if len(rows) < min_rows:
            raise ValueError(f"{local_file} yielded only {len(rows)} usable rows "
                             f"(need at least {min_rows})")
        toml.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return record

    if path.is_file() and toml.is_file():
        record = json.loads(toml.read_text(encoding="utf-8"))
        if record.get("sha256") == file_hash(path) and record.get("rows", 0) >= min_rows:
            record["reused_from_cache"] = True
            return record

    best, log = None, []
    for attempt in range(1, attempts + 1):
        try:
            content = _download(url, session)
        except RuntimeError as exc:
            log.append({"attempt": attempt, "error": str(exc)})
            continue
        if content.lstrip()[:1] == b"<":
            log.append({"attempt": attempt, "error": "server returned HTML, not a SMILES file"})
            continue
        path.write_bytes(content)
        rows = read_smi(path)
        entry = {"attempt": attempt, "bytes": len(content), "rows": len(rows),
                 "trailing_newline": content.endswith(b"\n")}
        log.append(entry)
        if best is None or len(rows) > best[0]:
            best = (len(rows), content, entry)
        if len(rows) >= min_rows and content.endswith(b"\n"):
            break
    if best is None or best[0] < min_rows:
        raise RuntimeError(f"Could not obtain at least {min_rows} {name} rows from {url}; "
                           f"attempts={log}")

    rows_count, content, chosen = best
    path.write_bytes(content)
    record = {
        "url": url, "sha256": file_hash(path), "bytes": len(content), "rows": rows_count,
        "trailing_newline": content.endswith(b"\n"),
        "partial_download": not (content.endswith(b"\n") and rows_count >= min_rows),
        "attempts": log, "chosen_attempt": chosen["attempt"],
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "local_path": str(path.resolve()), "reused_from_cache": False,
    }
    toml.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def read_smi(path: Path) -> list[dict]:
    """Read a DUD-E .ism file: `SMILES id [extra...]`, one molecule per line."""
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("<"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        rows.append({"id": parts[1], "smiles": parts[0]})
    return rows


def canonical(smiles: str) -> str | None:
    mol = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else None


def sample_rows(rows: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Deterministic sample: sort by id first so RNG draws are reproducible."""
    ordered = sorted(rows, key=lambda r: r["id"])
    if n >= len(ordered):
        return ordered
    return sorted(rng.sample(ordered, n), key=lambda r: r["id"])


# ------------------------------------------------------------------- statistics

def roc_auc(scores: np.ndarray, labels: np.ndarray) -> float:
    """Mann-Whitney U form of ROC-AUC; higher score must mean 'more likely active'."""
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if pos.size == 0 or neg.size == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(scores.size, dtype=float)
    ranks[order] = np.arange(1, scores.size + 1, dtype=float)
    sorted_scores = scores[order]
    start = 0
    for i in range(1, scores.size + 1):
        if i == scores.size or sorted_scores[i] != sorted_scores[start]:
            if i - start > 1:
                ranks[order[start:i]] = (start + 1 + i) / 2.0
            start = i
    return float((ranks[labels == 1].sum() - pos.size * (pos.size + 1) / 2.0) / (pos.size * neg.size))


def average_ranks(values: np.ndarray) -> np.ndarray:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(values.size, dtype=float)
    ranks[order] = np.arange(1, values.size + 1, dtype=float)
    sorted_values = values[order]
    start = 0
    for i in range(1, values.size + 1):
        if i == values.size or sorted_values[i] != sorted_values[start]:
            if i - start > 1:
                ranks[order[start:i]] = (start + 1 + i) / 2.0
            start = i
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 3:
        return float("nan")
    rx, ry = average_ranks(x), average_ranks(y)
    rx, ry = rx - rx.mean(), ry - ry.mean()
    denominator = math.sqrt(float((rx ** 2).sum()) * float((ry ** 2).sum()))
    return float((rx * ry).sum() / denominator) if denominator else float("nan")


def enrichment_factor(scores: np.ndarray, labels: np.ndarray, fraction: float) -> float | None:
    """EF = (actives in top x% / actives total) / (top x% size / panel size)."""
    total = scores.size
    n_actives = int(labels.sum())
    top_k = max(1, int(math.ceil(fraction * total)))
    if n_actives == 0:
        return None
    top_idx = np.argsort(-scores, kind="mergesort")[:top_k]
    hits = int(labels[top_idx].sum())
    return float((hits / n_actives) / (top_k / total))


def size_matched_auc(scores: np.ndarray, labels: np.ndarray, sizes: np.ndarray, bins: int = 4):
    """AUC computed inside heavy-atom bins that contain both classes, then weighted."""
    edges = np.unique(np.quantile(sizes, np.linspace(0, 1, bins + 1)))
    per_bin, weights, aucs = [], [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (sizes >= lo) & (sizes <= hi)
        n_a, n_d = int((mask & (labels == 1)).sum()), int((mask & (labels == 0)).sum())
        if n_a < 2 or n_d < 2:
            per_bin.append({"range": [float(lo), float(hi)], "n_actives": n_a,
                            "n_decoys": n_d, "auc": None})
            continue
        auc = roc_auc(scores[mask], labels[mask])
        per_bin.append({"range": [float(lo), float(hi)], "n_actives": n_a,
                        "n_decoys": n_d, "auc": auc})
        aucs.append(auc)
        weights.append(min(n_a, n_d))
    weighted = float(np.average(aucs, weights=weights)) if aucs else float("nan")
    return weighted, per_bin


# --------------------------------------------------------------------- docking

def run_panel(panel: list[dict], receptor, center, size, run_cfg, artifact_dir: Path, workers: int):
    """Concurrent Vina over the panel; results stay in panel order."""
    jobs = [(i, row["smiles"]) for i, row in enumerate(panel)]
    results: list[dict | None] = [None] * len(panel)

    def run_one(index, smiles):
        try:
            return index, dock_smiles(smiles, receptor, center, size, artifact_dir=artifact_dir, **run_cfg)
        except Exception as exc:  # tool failure must not abort the panel
            return index, {"smiles": smiles, "score": None, "valid": False,
                           "status": "tool_error", "error": f"{type(exc).__name__}: {exc}"}

    if workers <= 1:
        for index, smiles in jobs:
            i, result = run_one(index, smiles)
            results[i] = result
        return results

    with ThreadPoolExecutor(max_workers=min(workers, len(jobs)), thread_name_prefix="vina") as executor:
        futures = [executor.submit(run_one, index, smiles) for index, smiles in jobs]
        done = 0
        for future in as_completed(futures):
            index, result = future.result()
            results[index] = result
            done += 1
            if done % 10 == 0 or done == len(jobs):
                print(f"  docked {done}/{len(jobs)}", flush=True)
    return results


def descriptor_row(smiles: str) -> dict:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return {"heavy_atoms": None, "mw": None, "logp": None}
    return {"heavy_atoms": mol.GetNumHeavyAtoms(),
            "mw": round(Descriptors.MolWt(mol), 2),
            "logp": round(Descriptors.MolLogP(mol), 3)}


# ----------------------------------------------------------------------- plot

def write_plot(rows: list[dict], path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    scored = [r for r in rows if r["score"] is not None and r["heavy_atoms"]]
    if not scored:
        return False
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    colors = {"actives": "#d62728", "decoys": "#1f77b4", "anchors": "#2ca02c"}
    for group in ("decoys", "actives", "anchors"):
        subset = [r for r in scored if r["group"] == group]
        if not subset:
            continue
        axes[0].scatter([r["heavy_atoms"] for r in subset], [r["score"] for r in subset],
                        s=26, alpha=0.75, label=f"{group} (n={len(subset)})", color=colors[group])
    axes[0].set_xlabel("heavy atoms")
    axes[0].set_ylabel("Vina score (kcal/mol, lower = better)")
    axes[0].set_title("Size bias check: score vs heavy atoms")
    axes[0].legend(fontsize=8)
    axes[0].grid(alpha=0.25)
    for group in ("actives", "decoys"):
        subset = [r["score"] for r in scored if r["group"] == group]
        if subset:
            axes[1].hist(subset, bins=18, alpha=0.6, label=group, color=colors[group])
    axes[1].set_xlabel("Vina score (kcal/mol)")
    axes[1].set_ylabel("molecules")
    axes[1].set_title("Score distribution by group")
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return True


# ----------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", required=True, help="output directory (must not exist)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    parser.add_argument("--n-actives", type=int, default=40)
    parser.add_argument("--n-decoys", type=int, default=160)
    parser.add_argument("--sample-seed", type=int, default=2026)
    parser.add_argument("--actives-file", default=None,
                        help="use a local DUD-E-format actives file instead of downloading")
    parser.add_argument("--decoys-file", default=None,
                        help="use a local DUD-E-format decoys file instead of downloading")
    parser.add_argument("--min-decoys", type=int, default=500,
                        help="minimum decoy rows required before statistics are trusted")
    parser.add_argument("--download-attempts", type=int, default=4)
    parser.add_argument("--exhaustiveness", type=int, default=None, help="default: config scoring.vina")
    parser.add_argument("--n-poses", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None, help="Vina seed; default: config")
    parser.add_argument("--cpu", type=int, default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None, help="concurrent Vina processes")
    parser.add_argument("--no-anchors", action="store_true", help="skip erlotinib/gefitinib/afatinib")
    args = parser.parse_args(argv)

    output = Path(args.output)
    if output.exists():
        parser.exit(2, f"Output directory already exists: {output}\n")
    output.mkdir(parents=True)

    cfg = load_config(args.config)
    target = cfg["target"]
    vina_cfg = cfg.get("scoring", {}).get("vina", {}) or {}
    pocket = target["pocket"]
    center = tuple(pocket[f"center_{a}"] for a in "xyz")
    size = tuple(pocket[f"size_{a}"] for a in "xyz")

    run_cfg = {
        "exhaustiveness": args.exhaustiveness or int(vina_cfg.get("exhaustiveness", 8)),
        "n_poses": args.n_poses or int(vina_cfg.get("n_poses", 5)),
        "seed": args.seed if args.seed is not None else int(vina_cfg.get("seed", 2026)),
        "cpu": args.cpu or int(vina_cfg.get("cpu", 2)),
        "timeout": args.timeout or int(vina_cfg.get("timeout", 180)),
    }
    workers = args.workers or int(vina_cfg.get("workers", 4))

    receptor = target["receptor_pdbqt"]
    audit = validate_receptor(receptor)
    binary = resolve_vina_binary()

    session = requests.Session()
    cache_dir = Path(args.cache_dir)
    sources, panel, skipped = {}, [], []
    rng = random.Random(args.sample_seed)
    for group, key in (("actives", "actives"), ("decoys", "decoys")):
        record = ensure_dude_file(
            key, DUDE_SOURCES[key], cache_dir, session,
            attempts=args.download_attempts,
            min_rows=100 if group == "actives" else args.min_decoys,
            local_file=args.actives_file if group == "actives" else args.decoys_file,
        )
        rows = read_smi(Path(record["local_path"]))
        wanted = args.n_actives if group == "actives" else args.n_decoys
        picked = sample_rows(rows, wanted, rng)
        sources[group] = {**record, "available": len(rows), "sampled": len(picked),
                          "sample_seed": args.sample_seed, "requested": wanted}
        for row in picked:
            smi = canonical(row["smiles"])
            if smi is None:
                skipped.append({"group": group, "id": row["id"], "reason": "unparsable SMILES"})
                continue
            panel.append({"group": group, "id": row["id"], "smiles": smi,
                          "source": "DUD-E EGFR", "kind": "control"})

    if not args.no_anchors:
        for name, row in load_references().items():
            panel.append({"group": "anchors", "id": name, "smiles": row["smiles"],
                          "source": "PubChem (verified identity)", "kind": "named reference drug"})

    # Label leakage guard: an overlap would silently inflate enrichment.
    actives = {r["smiles"] for r in panel if r["group"] == "actives"}
    decoys = {r["smiles"] for r in panel if r["group"] == "decoys"}
    overlap = sorted(actives & decoys)
    if overlap:
        panel = [r for r in panel if not (r["group"] == "decoys" and r["smiles"] in set(overlap))]

    for row in panel:
        row.update(descriptor_row(row["smiles"]))

    print(f"panel: {len(panel)} molecules, workers={workers}, run_cfg={run_cfg}", flush=True)
    started = time.time()
    results = run_panel(panel, receptor, center, size, run_cfg, output / "docking", workers)
    elapsed = time.time() - started
    for row, result in zip(panel, results):
        row["score"] = result.get("score") if result else None
        row["status"] = result.get("status", "unknown") if result else "missing"
        row["error"] = result.get("error") if result else None
        row["artifact_dir"] = (result or {}).get("artifacts", {}).get("directory")

    with (output / "scores.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["group", "id", "smiles", "heavy_atoms",
                                                    "mw", "logp", "score", "status", "error",
                                                    "artifact_dir"])
        writer.writeheader()
        for row in panel:
            writer.writerow({k: row.get(k) for k in writer.fieldnames})

    # ---- statistics over molecules that produced a finite score
    scored = [r for r in panel if isinstance(r["score"], (int, float)) and math.isfinite(r["score"])]
    pair = [r for r in scored if r["group"] in ("actives", "decoys")]
    if len(pair) < 4:
        parser.exit(3, "Too few scored actives/decoys for enrichment statistics\n")
    scores = np.array([-r["score"] for r in pair], dtype=float)  # more positive = better binder
    labels = np.array([1 if r["group"] == "actives" else 0 for r in pair], dtype=int)
    sizes = np.array([r["heavy_atoms"] for r in pair], dtype=float)
    auc = roc_auc(scores, labels)
    matched_auc, per_bin = size_matched_auc(scores, labels, sizes)
    ef = {f"EF@{int(f * 100)}%": enrichment_factor(scores, labels, f) for f in (0.01, 0.05, 0.10, 0.20)}
    rho_all = spearman(np.array([r["heavy_atoms"] for r in scored], dtype=float),
                       np.array([r["score"] for r in scored], dtype=float))
    rho_act = spearman(np.array([r["heavy_atoms"] for r in pair if r["group"] == "actives"], dtype=float),
                       np.array([r["score"] for r in pair if r["group"] == "actives"], dtype=float))
    rho_dec = spearman(np.array([r["heavy_atoms"] for r in pair if r["group"] == "decoys"], dtype=float),
                       np.array([r["score"] for r in pair if r["group"] == "decoys"], dtype=float))

    def group_summary(group):
        rows = [r for r in scored if r["group"] == group]
        if not rows:
            return None
        values = np.array([r["score"] for r in rows], dtype=float)
        return {"n": len(rows), "median_score": float(np.median(values)),
                "mean_score": float(values.mean()), "std_score": float(values.std(ddof=1)) if len(rows) > 1 else None,
                "best_score": float(values.min()), "worst_score": float(values.max()),
                "median_heavy_atoms": float(np.median([r["heavy_atoms"] for r in rows]))}

    summary = {g: group_summary(g) for g in ("actives", "decoys", "anchors")}
    failed = [{"group": r["group"], "id": r["id"], "status": r["status"], "error": r["error"]}
              for r in panel if r["score"] is None]

    top_decile = sorted(pair, key=lambda r: r["score"])[:max(1, len(pair) // 10)]
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "question": "Under the current docking protocol, do known EGFR actives rank above "
                    "property-matched decoys?",
        "protocol": {
            "target": target, "run_cfg": run_cfg, "workers": workers,
            "receptor_sha256": file_hash(receptor),
            "receptor_audit": audit,
            "vina_binary_sha256": file_hash(binary),
            "docking_protocol_id": digest(docking_protocol(target, cfg.get("scoring", {}))),
            "run_signature": digest({"target": target, "run_cfg": run_cfg,
                                     "n_actives": args.n_actives, "n_decoys": args.n_decoys,
                                     "sample_seed": args.sample_seed}),
        },
        "external_data": sources,
        "panel": {"total": len(panel), "scored": len(scored), "actives_decoys_paired": len(pair),
                  "unparsable_skipped": skipped, "active_decoy_overlap_removed": overlap,
                  "dock_failures": failed, "wall_clock_seconds": round(elapsed, 1)},
        "enrichment": {"auc": auc, "size_matched_auc": matched_auc, "size_matched_bins": per_bin,
                       "enrichment_factors": ef},
        "size_bias": {"spearman_score_vs_heavy_atoms_all": rho_all,
                      "spearman_actives": rho_act, "spearman_decoys": rho_dec,
                      "median_heavy_atoms_top_decile": float(np.median([r["heavy_atoms"] for r in top_decile])),
                      "median_heavy_atoms_panel": float(np.median(sizes)),
                      "top_decile_actives": sum(1 for r in top_decile if r["group"] == "actives"),
                      "top_decile_size": len(top_decile)},
        "group_summary": summary,
        "rows": [{k: r.get(k) for k in ("group", "id", "smiles", "heavy_atoms", "mw", "logp",
                                        "score", "status")} for r in panel],
        "limitations": [
            "DUD-E decoys are property-matched but not experimentally confirmed inactive.",
            "Vina scores are uncalibrated and only support ranking within this fixed protocol.",
            "Afatinib is covalent; ordinary Vina does not model its covalent mechanism.",
            "Enrichment estimates carry wide uncertainty at this panel size; treat EF as "
            "directional, read the AUC with its sample size.",
        ] + ([
            "At least one panel file was truncated by the DUD-E host (no Content-Length, no "
            "Range support); the sampled frame is therefore partial. Re-run with "
            "--decoys-file/--actives-file pointing at a complete local copy to remove this caveat.",
        ] if any(s.get("partial_download") for s in sources.values()) else []),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    plot_written = write_plot(panel, output / "vina_vs_heavy_atoms.png")

    lines = [
        "# Control enrichment validation", "",
        f"- generated: {report['generated_at_utc']}",
        f"- protocol: exhaustiveness={run_cfg['exhaustiveness']}, n_poses={run_cfg['n_poses']}, "
        f"seed={run_cfg['seed']}, cpu={run_cfg['cpu']}, workers={workers}",
        f"- docking protocol id: `{report['protocol']['docking_protocol_id']}`",
        f"- panel: {len(panel)} molecules ({len(pair)} scored actives+decoys), "
        f"{len(failed)} docking failures, {round(elapsed, 1)}s wall clock", "",
        "## Enrichment", "",
        f"- ROC-AUC (actives vs decoys): **{auc:.3f}**",
        f"- size-matched ROC-AUC: **{matched_auc:.3f}**",
        "- " + ", ".join(f"{k}={v:.2f}" for k, v in ef.items() if v is not None), "",
        "## Score summary (kcal/mol, lower = better)", "",
        "| group | n | median | best | median heavy atoms |", "|---|---:|---:|---:|---:|",
    ]
    for group, stats in summary.items():
        if stats:
            lines.append(f"| {group} | {stats['n']} | {stats['median_score']:.3f} | "
                         f"{stats['best_score']:.3f} | {stats['median_heavy_atoms']:.0f} |")
    lines += [
        "", "## Size bias", "",
        f"- Spearman(score, heavy atoms) all scored: {rho_all:.3f}",
        f"- actives: {rho_act:.3f}, decoys: {rho_dec:.3f}",
        f"- median heavy atoms in top decile: {report['size_bias']['median_heavy_atoms_top_decile']:.0f} "
        f"vs panel {report['size_bias']['median_heavy_atoms_panel']:.0f}",
        f"- actives inside top decile: {report['size_bias']['top_decile_actives']}/"
        f"{report['size_bias']['top_decile_size']}", "",
        "## Reading guide", "",
        "AUC near 0.5 means docking does not separate the two groups under this protocol, so any",
        "score gain reported by the design loop cannot be attributed to binding quality. AUC above",
        "roughly 0.7 with a size-matched AUC of similar value is the minimum bar for treating Vina",
        "as a ranking signal here. See `report.json` for per-molecule rows and `scores.csv` for",
        "the raw table.", "",
        "## Limitations", "",
    ]
    lines += [f"- {item}" for item in report["limitations"]]
    if plot_written:
        lines += ["", "![Vina vs heavy atoms](vina_vs_heavy_atoms.png)"]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps({"output": str(output), "auc": round(auc, 4),
                      "size_matched_auc": round(matched_auc, 4),
                      "enrichment_factors": {k: (round(v, 3) if v else None) for k, v in ef.items()},
                      "panel": len(panel), "scored": len(scored), "failures": len(failed),
                      "median_actives": summary["actives"]["median_score"] if summary["actives"] else None,
                      "median_decoys": summary["decoys"]["median_score"] if summary["decoys"] else None,
                      "seconds": round(elapsed, 1)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
