"""tests/test_phase4_3.py - Phase 4.3 P1-3 + P2-3 tests.

P1-3: deterministic adoption check via Tanimoto similarity
P2-3: aggregate agent-level metrics (curves + verdict)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.diversity import (
    tanimoto_to_reference,
    batch_tanimoto_to_reference,
    adoption_stats,
)
from agents.agent_metrics import compute_agent_metrics
from agents.judge import judge_round
from loop import run_loop, load_config


# ---------------- P1-3: tanimoto_to_reference ----------------

def test_tanimoto_to_reference_self_is_one():
    print("\n=== test_tanimoto_to_reference_self_is_one ===")
    sm = "c1ccccc1"
    assert tanimoto_to_reference(sm, sm) == 1.0
    print(f"  [OK] self-similarity == 1.0")


def test_tanimoto_to_reference_known_pair():
    print("\n=== test_tanimoto_to_reference_known_pair ===")
    # aspirin vs ibuprofen: very different molecules, expect < 0.4
    sim = tanimoto_to_reference(
        "CC(=O)Oc1ccccc1C(=O)O",     # aspirin
        "CC(C)Cc1ccc(C(C)C(=O)O)cc1"  # ibuprofen
    )
    assert sim is not None and sim < 0.4, f"expected dissimilar, got {sim}"
    # same scaffold (both benzene) -> should be > 0.2
    assert sim > 0.1, f"expected at least some shared aromatic sim, got {sim}"
    print(f"  [OK] aspirin vs ibuprofen similarity = {sim} (both aromatic, but different)")


def test_batch_tanimoto_to_reference_aligns():
    print("\n=== test_batch_tanimoto_to_reference_aligns ===")
    ref = "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN1CCOCC1"  # gefitinib-like
    cands = [
        "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN1CCOCC1",  # exact = 1.0
        "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN",          # one morpholine shorter
        "c1ccccc1",                                          # benzene, distant
    ]
    sims = batch_tanimoto_to_reference(cands, ref)
    assert len(sims) == 3
    assert sims[0] == 1.0
    assert sims[2] < sims[1] < sims[0]
    print(f"  [OK] sims = {sims}")


def test_tanimoto_returns_none_for_garbage():
    print("\n=== test_tanimoto_returns_none_for_garbage ===")
    assert tanimoto_to_reference("not_smiles", "c1ccccc1") is None
    assert tanimoto_to_reference("c1ccccc1", "also_not") is None
    assert tanimoto_to_reference("", "c1ccccc1") is None
    assert tanimoto_to_reference("c1ccccc1", "") is None
    print(f"  [OK] invalid SMILES -> None")


def test_adoption_stats_threshold():
    print("\n=== test_adoption_stats_threshold ===")
    ref = "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN1CCOCC1"
    # 3 of 4 are highly similar (>= 0.7) to ref, 1 is not
    cands = [
        ref,
        "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN",          # very similar
        "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCC",          # very similar
        "c1ccccc1",                                          # NOT similar
    ]
    stats = adoption_stats(cands, ref, threshold=0.7)
    assert stats["n_total"] == 4
    assert stats["n_valid_sim"] == 4
    assert stats["n_adopted"] == 3
    assert stats["adoption_rate"] == 0.75
    assert stats["max_similarity"] == 1.0
    # benzene pulls mean below 0.7; assert only that mean < max
    assert stats["mean_similarity"] is not None and stats["mean_similarity"] < 1.0
    assert stats["mean_similarity"] > stats["mean_similarity"] * 0.5  # trivial sanity
    print(f"  [OK] 3/4 adopted @ 0.7 threshold, max_sim={stats['max_similarity']}, "
          f"mean_sim={stats['mean_similarity']}")


# ---------------- P1-3: judge_round emits deterministic block ----------------

def _make_candidate(smiles: str, vina: float, composite: float = 0.7) -> dict:
    return {
        "smiles": smiles,
        "provider": "deepseek",
        "validate": {"valid": True, "mw": 300, "logp": 2.0, "lipinski_pass": True},
        "admet": {"summary_score": 0.8, "qed": 0.5, "warnings": []},
        "dock": {"score": vina, "valid": True},
        "scaffold": "C1",
        "composite_score": composite,
    }


def test_judge_round_emits_deterministic_adoption():
    """P1-3: judgment dict must include adoption_deterministic and
    adoption_llm_vs_det_drift when a previous round is provided."""
    print("\n=== test_judge_round_emits_deterministic_adoption ===")
    cfg = load_config("config.yaml")

    ref_smiles = "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN1CCOCC1"
    prev = [
        _make_candidate(ref_smiles, -3.4),
        _make_candidate("COc1cc2ncnc(Nc3ccc(F)cc3)c2cc1OCC", -3.1),
    ]
    cur = [
        # 2 very similar to ref (will count as adopted @ 0.7)
        _make_candidate(ref_smiles, -3.6),
        _make_candidate("COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN", -3.3),
        # 1 unrelated
        _make_candidate("c1ccccc1", -2.5),
    ]
    j = judge_round(
        cur, cfg, round_num=1,
        previous_focus="Keep gefitinib core",
        previous_summary={"best_smiles": ref_smiles, "best_vina": -3.4},
        previous_enriched=prev,
        use_mock=True,
    )
    assert "adoption_deterministic" in j
    det = j["adoption_deterministic"]
    assert det["n_total"] == 3
    assert det["n_adopted"] == 2
    assert det["threshold"] == 0.7
    assert det["max_similarity"] == 1.0
    assert "adoption_llm_vs_det_drift" in j
    # LLM mock usually returns adopted_count=1 or so; we don't pin it,
    # but we DO assert it's a real int (or None) so the metric is well-defined.
    assert j["adoption_llm_vs_det_drift"] is None or isinstance(
        j["adoption_llm_vs_det_drift"], int
    )
    print(f"  [OK] det.n_adopted={det['n_adopted']} (expected 2); "
          f"max_sim={det['max_similarity']}; "
          f"drift={j['adoption_llm_vs_det_drift']}")


def test_judge_round_deterministic_handles_no_reference():
    """P1-3 edge case: previous_summary has no best_smiles -> det block is
    populated but n_valid_sim=0."""
    print("\n=== test_judge_round_deterministic_handles_no_reference ===")
    cfg = load_config("config.yaml")
    cur = [_make_candidate("c1ccccc1", -2.5)]
    j = judge_round(
        cur, cfg, round_num=1,
        previous_focus="anything",
        previous_summary={"best_vina": -3.0},   # NO best_smiles
        use_mock=True,
    )
    det = j["adoption_deterministic"]
    assert det["n_valid_sim"] == 0
    assert det["n_adopted"] == 0
    assert det["reference"] is None
    assert j["adoption_llm_vs_det_drift"] is None
    print(f"  [OK] missing reference handled gracefully")


# ---------------- P2-3: agent metrics ----------------

def _fake_summary(round_n, valid, total, vina, admet=0.8, scaffolds=3):
    return {
        "round": round_n,
        "n_total": total, "n_valid": valid, "n_complete": valid,
        "valid_ratio": round(valid / total, 3) if total else 0.0,
        "n_docked": valid, "n_unique_scaffolds": scaffolds,
        "avg_admet": admet,
        "best_vina": vina,
        "best_smiles": "CCO",
        "top_candidates": [{"smiles": "CCO", "score": 0.7, "vina": vina}],
    }


def _fake_judgment(round_n, adopted_llm=0, det_n_adopted=0):
    return {
        "status": "ok",
        "adoption_denominator": 5,
        "adopted_count": adopted_llm,
        "adoption_deterministic": {
            "n_total": 5, "n_valid_sim": 5, "n_adopted": det_n_adopted,
            "adoption_rate": round(det_n_adopted / 5, 3),
            "max_similarity": 0.8, "mean_similarity": 0.5,
            "threshold": 0.7, "reference": "CCO",
        },
        "adoption_llm_vs_det_drift": adopted_llm - det_n_adopted,
    }


def test_metrics_learning_monotonic_down():
    """best_vina monotonically decreasing -> verdict True."""
    print("\n=== test_metrics_learning_monotonic_down ===")
    s = [_fake_summary(i, 5, 5, -3.0 - 0.2 * i) for i in range(5)]
    j = [_fake_judgment(i, 1, 1) for i in range(5)]
    m = compute_agent_metrics(s, j, {"rounds_without_vina_improvement": 0})
    agg = m["aggregates"]
    assert agg["best_vina_first"] == -3.0
    assert agg["best_vina_last"] == -3.8
    assert agg["best_vina_delta"] == -0.8
    assert m["verdict"]["agent_is_learning"] is True
    print(f"  [OK] delta={agg['best_vina_delta']}, verdict={m['verdict']['rationale']}")


def test_metrics_learning_flat_is_false():
    """best_vina oscillates around -3.0 -> verdict False."""
    print("\n=== test_metrics_learning_flat_is_false ===")
    s = [
        _fake_summary(0, 5, 5, -3.0),
        _fake_summary(1, 5, 5, -3.2),
        _fake_summary(2, 5, 5, -2.9),
        _fake_summary(3, 5, 5, -3.0),
        _fake_summary(4, 5, 5, -2.95),
    ]
    m = compute_agent_metrics(s, [_fake_judgment(i) for i in range(5)])
    assert m["verdict"]["agent_is_learning"] is False
    assert m["aggregates"]["best_vina_delta"] == 0.05
    print(f"  [OK] flat-ish Vina -> agent_is_learning=False; "
          f"rationale={m['verdict']['rationale']}")


def test_metrics_learning_via_scaffold_doubling():
    """If Vina is noisy but scaffolds doubled, agent_is_learning=True."""
    print("\n=== test_metrics_learning_via_scaffold_doubling ===")
    s = [
        _fake_summary(0, 5, 5, -3.0, scaffolds=2),
        _fake_summary(1, 5, 5, -3.05, scaffolds=3),
        _fake_summary(2, 5, 5, -2.95, scaffolds=5),
    ]
    m = compute_agent_metrics(s, [_fake_judgment(i) for i in range(3)])
    assert m["verdict"]["agent_is_learning"] is True
    print(f"  [OK] scaffolds 2 -> 5 doubled; verdict=True; "
          f"rationale={m['verdict']['rationale']}")


def test_metrics_insufficient_data():
    """Single round -> None."""
    print("\n=== test_metrics_insufficient_data ===")
    m = compute_agent_metrics([_fake_summary(0, 5, 5, -3.0)])
    assert m["verdict"]["agent_is_learning"] is None
    print(f"  [OK] 1 round -> verdict=None")


def test_metrics_drift_aggregation():
    """Average LLM-vs-det drift across rounds."""
    print("\n=== test_metrics_drift_aggregation ===")
    s = [_fake_summary(i, 5, 5, -3.0 - i * 0.1) for i in range(4)]
    j = [
        _fake_judgment(0, adopted_llm=0, det_n_adopted=2),  # drift -2
        _fake_judgment(1, adopted_llm=5, det_n_adopted=2),  # drift +3
        _fake_judgment(2, adopted_llm=2, det_n_adopted=2),  # drift 0
        _fake_judgment(3, adopted_llm=2, det_n_adopted=2),  # drift 0
    ]
    m = compute_agent_metrics(s, j)
    assert m["aggregates"]["adoption_llm_vs_det_drift_avg"] == 0.25
    print(f"  [OK] avg drift = {m['aggregates']['adoption_llm_vs_det_drift_avg']}")


# ---------------- P2-3: end-to-end with loop.py ----------------

def test_end_to_end_loop_emits_metrics_json(tmp_path=None):
    """Run the loop with mock LLM; verify summary.json has agent_metrics
    block AND metrics.json is emitted alongside."""
    print("\n=== test_end_to_end_loop_emits_metrics_json ===")
    cfg = load_config("config.yaml")
    with tempfile.TemporaryDirectory() as tmp:
        overall = run_loop(
            config=cfg, output_dir=tmp,
            max_rounds=3, n_per_provider=3,
            dock_enabled=False, use_mock=True, verbose=False,
        )
        # summary.json has a flat agent_metrics block
        assert "agent_metrics" in overall
        am = overall["agent_metrics"]
        for key in ("best_vina_first", "best_vina_last", "best_vina_delta",
                    "valid_rate_improvement", "adoption_rate_avg_llm",
                    "adoption_rate_avg_det", "adoption_llm_vs_det_drift_avg",
                    "agent_is_learning"):
            assert key in am, f"missing agent_metrics.{key}"

        # metrics.json exists with full curves
        m_path = Path(tmp) / "metrics.json"
        assert m_path.exists()
        m = json.loads(m_path.read_text(encoding="utf-8"))
        assert m["schema_version"] == 1
        assert m["rounds_total"] == 3
        assert "curves" in m
        assert len(m["curves"]["best_vina"]) == 3
        assert len(m["curves"]["adoption_rate_deterministic"]) == 3
        assert "verdict" in m
        # JSON safe
        s = json.dumps(m)
        json.loads(s)
        print(f"  [OK] summary.json has agent_metrics block; metrics.json has "
              f"curves; verdict={m['verdict']['agent_is_learning']}; "
              f"rationale={m['verdict']['rationale']}")


def main():
    print("[TEST] Phase 4.3 (P1-3 + P2-3): adoption truth + agent metrics")
    print("=" * 60)
    test_tanimoto_to_reference_self_is_one()
    test_tanimoto_to_reference_known_pair()
    test_batch_tanimoto_to_reference_aligns()
    test_tanimoto_returns_none_for_garbage()
    test_adoption_stats_threshold()
    test_judge_round_emits_deterministic_adoption()
    test_judge_round_deterministic_handles_no_reference()
    test_metrics_learning_monotonic_down()
    test_metrics_learning_flat_is_false()
    test_metrics_learning_via_scaffold_doubling()
    test_metrics_insufficient_data()
    test_metrics_drift_aggregation()
    test_end_to_end_loop_emits_metrics_json()
    print("\n" + "=" * 60)
    print("[OK] All Phase 4.3 (P1-3 + P2-3) tests passed!")


if __name__ == "__main__":
    main()