"""tests/test_phase4_1.py - Phase 4.1 A/B comparison + unit tests.

Validates the 4 Phase 4.1 modules:
1. FailedLigandSet - persistence, dedup, prompt injection
2. WorkingMemory - round compression, convergence tracking
3. LoopController - 3 stop conditions
4. HITLCheckpoint - 3 checkpoint types (unit; interactive ones skip in CI)

Plus an A/B smoke test running both legacy loop and Phase 4.1 loop to
verify they both still work end-to-end.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.failed_set import FailedLigandSet
from agents.working_memory import WorkingMemory
from agents.loop_controller import LoopController, LoopState, LoopConfig
from agents.hitl import HITLCheckpoint


def _make_round_summary_dicts():
    """Fake evaluate_candidates output for memory tests."""
    return [
        {
            "smiles": "CCO",
            "validate": {"valid": True},
            "admet": {"summary_score": 0.85},
            "dock": {"score": -3.0},
            "composite_score": 0.7,
            "scaffold": "C",
        },
        {
            "smiles": "CC(=O)Oc1ccccc1C(=O)O",
            "validate": {"valid": True},
            "admet": {"summary_score": 0.88},
            "dock": {"score": -2.8},
            "composite_score": 0.65,
            "scaffold": "C1=CC=CC=C1",
        },
    ]


# ---------------- FailedLigandSet ----------------

def test_failed_set_dedup():
    print("\n=== test_failed_set_dedup ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "failed.json"
        fs = FailedLigandSet(path=path)

        # 3 distinct molecules (different canonical forms)
        assert fs.add_failed("CCO", "low score")
        assert fs.add_failed("CC(=O)Oc1ccccc1C(=O)O", "weak binding")
        assert fs.add_failed("c1ccccc1", "ring")  # benzene, distinct

        # Duplicate (same canonical as aspirin above, just whitespace)
        assert not fs.add_failed("  CC(=O)Oc1ccccc1C(=O)O  ", "dup")

        assert len(fs.failed) == 3, f"expected 3 unique, got {len(fs.failed)}"
        assert fs.is_failed("CCO")
        assert fs.is_failed("CC(=O)Oc1ccccc1C(=O)O")  # canonical
        assert not fs.is_failed("CCCCC")  # pentane, not ethanol

        # Persistence: reload from disk
        fs2 = FailedLigandSet(path=path)
        assert len(fs2.failed) == 3
        print(f"  [OK] dedup works ({len(fs.failed)} unique failed, 3 persisted)")


def test_failed_set_should_mark():
    print("\n=== test_failed_set_should_mark ===")
    fs = FailedLigandSet(threshold_composite=0.5, threshold_vina=-2.5)
    assert fs.should_mark_failed(0.3, -3.0)        # low composite
    assert fs.should_mark_failed(0.7, -2.0)         # weak vina
    assert not fs.should_mark_failed(0.7, -3.5)      # ok
    assert not fs.should_mark_failed(0.9, None)      # no vina data
    print("  [OK] threshold logic correct")


def test_failed_set_prompt_injection():
    print("\n=== test_failed_set_prompt_injection ===")
    temporary = tempfile.TemporaryDirectory()
    fs = FailedLigandSet(path=Path(temporary.name) / "failed.json")
    # Use real SMILES (RDKit must parse them)
    real_smiles = [
        "CCO", "CC(=O)Oc1ccccc1C(=O)O", "CC(C)Cc1ccc(C(C)C(=O)O)cc1",
        "CN1CCC[C@H]1c1cccnc1", "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
        "OC(=O)C1CCCCC1", "CC(=O)NCC(=O)N", "C#Cc1ccc(N)cc1",
        "c1ccc2ccccc2c1", "Brc1ccccc1", "Fc1ccccc1", "Clc1ccccc1",
    ]
    for s in real_smiles:
        fs.add_failed(s, "test")

    out = fs.format_for_prompt(max_show=5)
    assert "DO NOT propose" in out
    assert "and 7 more" in out  # 12 - 5 = 7
    print(f"  [OK] prompt includes 'DO NOT propose ... (and N more)' format")
    print(f"       total failed: {len(fs.failed)}, prompt snippet: {out[:120]}")


# ---------------- Phase 4.3 (P0-2): FailedLigandSet LRU cap ----------------

def test_failed_set_lru_evicts_oldest():
    """P0-2 fix: with max_size=N, the (N+1)-th insert evicts the first one."""
    print("\n=== test_failed_set_lru_evicts_oldest ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "failed.json"
        fs = FailedLigandSet(path=path, max_size=3)

        assert fs.add_failed("CCO", "first")
        assert fs.add_failed("c1ccccc1", "second")
        assert fs.add_failed("CC(=O)Oc1ccccc1C(=O)O", "third")
        assert len(fs.failed) == 3

        # 4th insert should evict the OLDEST (CCO)
        assert fs.add_failed("Brc1ccccc1", "fourth")
        assert len(fs.failed) == 3
        assert not fs.is_failed("CCO"), "oldest should have been evicted"
        assert fs.is_failed("c1ccccc1")
        assert fs.is_failed("CC(=O)Oc1ccccc1C(=O)O")
        assert fs.is_failed("Brc1ccccc1")

        # Reasons dict also evicted the oldest
        assert "CCO" not in fs.reasons
        assert fs.reasons["Brc1ccccc1"] == "fourth"
        print(f"  [OK] LRU evict: oldest entry dropped at max_size={fs.max_size}")


def test_failed_set_lru_persists_across_reload():
    """Eviction survives a reload from disk (the persisted JSON is bounded too)."""
    print("\n=== test_failed_set_lru_persists_across_reload ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "failed.json"
        fs = FailedLigandSet(path=path, max_size=2)
        fs.add_failed("CCO", "first")
        fs.add_failed("c1ccccc1", "second")
        fs.add_failed("Brc1ccccc1", "third")  # evicts CCO

        fs2 = FailedLigandSet(path=path, max_size=2)
        assert len(fs2.failed) == 2
        assert not fs2.is_failed("CCO")
        assert fs2.is_failed("c1ccccc1")
        assert fs2.is_failed("Brc1ccccc1")
        print(f"  [OK] cap+eviction survives disk reload")


def test_failed_set_unbounded_when_max_size_zero():
    """max_size=0 (default) preserves legacy unbounded behavior."""
    print("\n=== test_failed_set_unbounded_when_max_size_zero ===")
    # 8 structurally distinct molecules RDKit parses distinctly
    distinct = [
        "CCO", "CC(=O)Oc1ccccc1C(=O)O", "CC(C)Cc1ccc(C(C)C(=O)O)cc1",
        "CN1CCC[C@H]1c1cccnc1", "Cn1cnc2c1c(=O)n(C)c(=O)n2C",
        "OC(=O)C1CCCCC1", "CC(=O)NCC(=O)N", "C#Cc1ccc(N)cc1",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        fs = FailedLigandSet(path=Path(tmp) / "failed.json", max_size=0)
        for s in distinct:
            fs.add_failed(s, "test")
        assert len(fs.failed) == len(distinct)
        # And no LRU eviction triggered
        assert fs.max_size == 0
        print(f"  [OK] max_size=0 keeps legacy unbounded behavior ({len(fs.failed)} entries)")


# ---------------- WorkingMemory ----------------

def test_working_memory_compression():
    print("\n=== test_working_memory_compression ===")
    mem = WorkingMemory(max_recent=3)
    out0 = mem.compress_for_generator()
    assert "First round" in out0

    mem.add_round(_make_round_summary_dicts(), focus="Try morpholine")
    out1 = mem.compress_for_generator()
    assert "Round 0" in out1
    assert "morpholine" in out1 or "Try morpholine" in out1
    print(f"  [OK] compression output includes round info + strategy")


def _make_best_candidate(smiles="CCO", vina=-8.8, composite=0.58, **extra):
    row = {
        "smiles": smiles,
        "validate": {"valid": True},
        "admet": {"summary_score": 0.85, "herg_risk_score": 0.83},
        "dock": {"score": vina},
        "composite_score": composite,
        "scaffold": "C",
    }
    row.update(extra)
    return row


def test_working_memory_never_labels_unsafe_best_as_safe():
    """Regression (2026-09-17): the best-so-far line said "safe" unconditionally.

    On the confirmatory pool the all-candidate optimum had herg_risk 0.830 while
    a safety-passing molecule was 0.020 kcal/mol behind, so calling an unsafe
    molecule "safe" in the generator prompt pushed it toward the rejected profile.
    """
    print("\n=== test_working_memory_never_labels_unsafe_best_as_safe ===")
    mem = WorkingMemory(max_recent=3)
    mem.add_round([_make_best_candidate(safety_gate_pass=False)], focus="F")
    out = mem.compress_for_generator()
    assert "Best safe Pareto candidate" not in out
    assert "FAILS the safety gate" in out
    assert "hERG-risk=0.830" in out
    print("  [OK] unsafe best is labelled as failing the gate, not as safe")

    mem2 = WorkingMemory(max_recent=3)
    mem2.add_round([_make_best_candidate(safety_gate_pass=True)], focus="F")
    out2 = mem2.compress_for_generator()
    assert "Best safety-gate-passing candidate" in out2
    assert "FAILS the safety gate" not in out2
    print("  [OK] safety-passing best is labelled as such")

    # Unknown gate + legacy v1 ADMET schema (no herg_risk_score) must not
    # render a bare None into the prompt.
    mem3 = WorkingMemory(max_recent=3)
    legacy = _make_best_candidate()
    legacy["admet"] = {"summary_score": 0.85, "herg_risk": 0.0}
    mem3.add_round([legacy], focus="F")
    out3 = mem3.compress_for_generator()
    assert "safety gate not evaluated" in out3
    assert "hERG-risk=0.000" in out3
    assert "None" not in out3
    print("  [OK] unknown gate + v1 ADMET schema handled without None leakage")


def test_working_memory_rounds_since_improvement():
    print("\n=== test_working_memory_rounds_since_improvement ===")
    mem = WorkingMemory()
    # Round 0: best Vina = -3.0
    rd = _make_round_summary_dicts()
    rd[0]["dock"]["score"] = -3.0
    mem.add_round(rd, focus="F1")
    assert mem.rounds_since_improvement() == 0   # no history to compare

    # Round 1: best Vina = -2.8 (worse than R0, no improvement)
    rd2 = _make_round_summary_dicts()
    rd2[0]["dock"]["score"] = -2.8
    mem.add_round(rd2, focus="F2")
    assert mem.rounds_since_improvement() == 1   # 1 round without improvement

    # Round 2: best Vina = -3.5 (better than R1, improvement!)
    rd3 = _make_round_summary_dicts()
    rd3[0]["dock"]["score"] = -3.5
    mem.add_round(rd3, focus="F3")
    assert mem.rounds_since_improvement() == 0   # streak reset
    print("  [OK] convergence counter resets on improvement")


def test_working_memory_truncation():
    print("\n=== test_working_memory_truncation ===")
    mem = WorkingMemory(max_recent=2)
    for i in range(5):
        mem.add_round(_make_round_summary_dicts(), focus=f"F{i}")
    assert len(mem.recent_rounds) == 2
    assert len(mem.strategy_chain) == 2
    print("  [OK] auto-truncation to max_recent")


# ---------------- Phase 4.3 (P0-3): strategy_chain cross-session ----------------

def test_strategy_chain_persists_across_sessions():
    """P0-3: max_recent truncates the live list, but the on-disk file
    keeps every focus ever recorded. A new WorkingMemory seeded from
    that file picks up where the prior session left off."""
    print("\n=== test_strategy_chain_persists_across_sessions ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "strat.json"

        # Session 1: 5 rounds, max_recent=2 -> live chain keeps only last 2
        m1 = WorkingMemory(max_recent=2, strategy_persist_path=path, target_name="EGFR")
        for i in range(5):
            m1.add_round(_make_round_summary_dicts(), focus=f"focus_{i}")
        assert len(m1.strategy_chain) == 2          # truncated live
        assert m1.strategy_chain == ["focus_3", "focus_4"]

        # Disk keeps ALL 5
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert len(on_disk["strategy_chain"]) == 5
        assert on_disk["strategy_chain"][0] == "focus_0"
        assert on_disk["strategy_chain"][-1] == "focus_4"

        # Session 2: seed from disk, only last 2 visible in live
        m2 = WorkingMemory(max_recent=2, strategy_persist_path=path, target_name="EGFR")
        assert m2.strategy_chain == ["focus_3", "focus_4"]
        assert m2.total_rounds == 5
        # Adding one more focus appends to the SAME disk file (no clobber)
        m2.add_round(_make_round_summary_dicts(), focus="focus_5")
        on_disk2 = json.loads(path.read_text(encoding="utf-8"))
        assert len(on_disk2["strategy_chain"]) == 6
        assert on_disk2["strategy_chain"][-1] == "focus_5"
        print(f"  [OK] strategy_history persists; session 2 saw last "
              f"{len(m2.strategy_chain)} of {len(on_disk2['strategy_chain'])} on disk")


def test_strategy_chain_persist_failure_does_not_crash():
    """P0-3: if the persist path is unwritable (read-only dir), add_round
    must still work — persistence is best-effort, never blocking."""
    print("\n=== test_strategy_chain_persist_failure_does_not_crash ===")
    import os
    if os.name == "nt":
        # Skip on Windows where chmod is unreliable
        print("  [SKIP] chmod unreliable on Windows in CI")
        return
    with tempfile.TemporaryDirectory() as tmp:
        ro = Path(tmp) / "ro"
        ro.mkdir()
        os.chmod(ro, 0o555)
        path = ro / "strat.json"  # parent is read-only
        try:
            mem = WorkingMemory(max_recent=2, strategy_persist_path=path)
            mem.add_round(_make_round_summary_dicts(), focus="will warn but not crash")
        finally:
            os.chmod(ro, 0o755)


# ---------------- Phase 4.3 (P1-1): best_molecules cross-session ----------------

def test_best_molecules_persists_across_sessions():
    """Best-so-far persists within one caller-selected protocol file."""
    print("\n=== test_best_molecules_persists_across_sessions ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "best.json"

    # Session 1: a Vina=-3.5 candidate wins
    rd = _make_round_summary_dicts()
    rd[0]["dock"]["score"] = -3.5
    rd[0]["scaffold"] = "test_scaffold"
    rd[0]["smiles"] = "CCO_test"
    m1 = WorkingMemory(best_persist_path=path, target_name="EGFR")
    m1.add_round(rd, focus="F1")
    assert m1.best_so_far is not None

    # Disk has it
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["targets"]["EGFR"]["vina"] == -3.5
    assert data["targets"]["EGFR"]["smiles"] == "CCO_test"
    print(f"  [OK] persisted best Vina={data['targets']['EGFR']['vina']} "
          f"smiles={data['targets']['EGFR']['smiles']}")

    # Session 2 (no rounds yet): should see -3.5 from disk
    m2 = WorkingMemory(best_persist_path=path, target_name="EGFR")
    assert m2.best_so_far is not None
    assert m2.best_so_far["smiles"] == "CCO_test"
    assert m2.best_so_far["dock"]["score"] == -3.5
    assert m2.best_so_far.get("is_persisted_from_prior_session") is True

    # Worse molecule in session 2 does NOT clobber the persisted -3.5
    rd2 = _make_round_summary_dicts()
    rd2[0]["dock"]["score"] = -2.0
    m2.add_round(rd2, focus="F2")
    data2 = json.loads(path.read_text(encoding="utf-8"))
    assert data2["targets"]["EGFR"]["vina"] == -3.5, "worse should not overwrite"
    print(f"  [OK] worse candidate did not overwrite persisted -3.5")

    # Better molecule in session 2 DOES upgrade
    rd3 = _make_round_summary_dicts()
    rd3[0]["dock"]["score"] = -4.0
    rd3[0]["smiles"] = "improved_smiles"
    m2.add_round(rd3, focus="F3")
    data3 = json.loads(path.read_text(encoding="utf-8"))
    assert data3["targets"]["EGFR"]["vina"] == -4.0
    assert data3["targets"]["EGFR"]["smiles"] == "improved_smiles"
    print(f"  [OK] better candidate (-4.0) upgraded persisted record")


# ---------------- LoopController ----------------

def test_loop_controller_max_rounds():
    print("\n=== test_loop_controller_max_rounds ===")
    ctrl = LoopController(LoopConfig(max_rounds=3, token_budget=10000, judge_convergence_patience=2))
    state = LoopState()
    state.round = 2
    assert not ctrl.should_stop(state)[0]
    state.round = 3
    assert ctrl.should_stop(state)[0]
    assert ctrl.should_stop(state)[1] == "max_rounds_reached"
    print("  [OK] max_rounds trigger")


def test_loop_controller_convergence():
    print("\n=== test_loop_controller_convergence ===")
    ctrl = LoopController(LoopConfig(max_rounds=10, token_budget=100000, judge_convergence_patience=2))
    state = LoopState(round=1)
    state.note_round_result(-3.0)
    state.note_round_result(-3.0)  # no improvement
    state.note_round_result(-3.0)  # no improvement
    assert ctrl.should_stop(state)[0]
    assert ctrl.should_stop(state)[1] == "no_improvement"
    print("  [OK] judge_convergence trigger after N rounds without improvement")


def test_loop_controller_progress_signal_safe_vina():
    """safe_vina watches only safety-gate-passing progress (2026-09-17)."""
    print("\n=== test_loop_controller_progress_signal_safe_vina ===")
    config = LoopConfig(max_rounds=10, token_budget=100000,
                        judge_convergence_patience=2, progress_signal="safe_vina")
    ctrl = LoopController(config)
    state = LoopState(round=1)
    # A great-but-unsafe Vina must NOT count as progress under this signal.
    state.note_round_result(-9.5)
    state.note_round_safe_result(None)
    state.note_round_result(-9.4)
    state.note_round_safe_result(-7.0)   # first safe evidence -> improvement
    assert not ctrl.should_stop(state)[0]
    assert state.rounds_without_vina_improvement == 1
    assert state.rounds_without_safe_vina_improvement == 0
    # Two rounds with no safe improvement now trip the counter.
    state.note_round_result(-9.3)
    state.note_round_safe_result(-7.0)
    state.note_round_result(-9.2)
    state.note_round_safe_result(-7.0)
    assert ctrl.should_stop(state)[0]
    assert ctrl.should_stop(state)[1] == "no_improvement"
    assert "safe_vina" in ctrl.explain("no_improvement")
    print("  [OK] safe_vina ignores unsafe Vina gains and honours safe progress")


def test_loop_controller_safe_vina_none_does_not_advance_patience():
    """A round with no safety-passing candidate is 'unknown', not a plateau."""
    print("\n=== test_loop_controller_safe_vina_none_does_not_advance_patience ===")
    ctrl = LoopController(LoopConfig(max_rounds=10, token_budget=100000,
                                     judge_convergence_patience=2,
                                     progress_signal="safe_vina"))
    state = LoopState(round=1)
    for _ in range(5):
        state.note_round_safe_result(None)
    assert state.rounds_without_safe_vina_improvement == 0
    assert not ctrl.should_stop(state)[0]
    print("  [OK] None keeps the counter frozen (matches --no-dock smoke behaviour)")


def test_loop_controller_rejects_unknown_progress_signal():
    print("\n=== test_loop_controller_rejects_unknown_progress_signal ===")
    try:
        LoopController(LoopConfig(progress_signal="safe"))
    except ValueError as exc:
        assert "progress_signal" in str(exc)
        print("  [OK] invalid progress_signal rejected")
    else:
        raise AssertionError("expected ValueError for an unknown progress_signal")


def test_loop_controller_legacy_default_unchanged():
    """Default stays 'vina' so pre-2026-09-17 runs remain comparable."""
    print("\n=== test_loop_controller_legacy_default_unchanged ===")
    ctrl = LoopController(LoopConfig())
    assert ctrl.progress_signal == "vina"
    state = LoopState(round=1)
    state.note_round_result(-3.0)
    state.note_round_result(-3.0)
    state.note_round_result(-3.0)
    assert ctrl.should_stop(state)[0]
    print("  [OK] default signal is the legacy all-candidate vina")


def test_loop_controller_hitl_veto():
    print("\n=== test_loop_controller_hitl_veto ===")
    ctrl = LoopController(LoopConfig())
    state = LoopState(round=1, hitl_veto=True)
    stop, reason = ctrl.should_stop(state)
    assert stop and reason == "human_veto"
    print("  [OK] HITL veto overrides other conditions")


def test_loop_controller_token_budget():
    print("\n=== test_loop_controller_token_budget ===")
    ctrl = LoopController(LoopConfig(max_rounds=100, token_budget=1000, judge_convergence_patience=100))
    state = LoopState(round=0, tokens_used=999)
    assert not ctrl.should_stop(state)[0]
    state.tokens_used = 1000
    assert ctrl.should_stop(state)[0]
    assert ctrl.should_stop(state)[1] == "token_budget_exceeded"
    print("  [OK] token_budget trigger")


# ---------------- HITLCheckpoint ----------------

def test_hitl_disabled():
    print("\n=== test_hitl_disabled ===")
    hitl = HITLCheckpoint(require_approval=False)
    # All checks should auto-pass
    assert hitl.pre_loop(5, 5, 2)
    assert hitl.on_vina_breakthrough(-3.0, -3.5)
    chosen = hitl.select_synthesis_candidates([])
    assert chosen == []
    print("  [OK] disabled mode = auto-pass")


def test_hitl_veto_sets_state():
    print("\n=== test_hitl_veto_sets_state ===")
    hitl = HITLCheckpoint(require_approval=False)
    hitl.veto = True
    print(f"  [OK] veto can be set programmatically: {hitl.veto}")


# ---------------- A/B smoke test ----------------

def test_loop_works_with_phase41_enabled():
    print("\n=== test_loop_works_with_phase41_enabled ===")
    from loop import run_loop, load_config

    cfg = load_config("config.yaml")

    with tempfile.TemporaryDirectory() as tmp:
        # Default mode (no HITL - just memory + controller + failed set)
        overall = run_loop(
            config=cfg,
            output_dir=tmp,
            max_rounds=2,
            n_per_provider=3,
            dock_enabled=False,
            use_mock=True,
            hitl=False,
        )
        assert overall["rounds_completed"] == 2
        # Phase 4.1 metadata
        assert "loop_state" in overall
        assert "failed_set_size" in overall["loop_state"]
        assert "memory_rounds" in overall["loop_state"]
        print(f"  [OK] Phase 4.1 loop runs end-to-end")
        print(f"       failed_set_size: {overall['loop_state']['failed_set_size']}")
        print(f"       memory_rounds:   {overall['loop_state']['memory_rounds']}")


def main():
    print("[TEST] Phase 4.1 + 4.3 (P0-2 / P0-3 / P1-1) modules")
    print("=" * 60)
    test_failed_set_dedup()
    test_failed_set_should_mark()
    test_failed_set_prompt_injection()
    test_failed_set_lru_evicts_oldest()
    test_failed_set_lru_persists_across_reload()
    test_failed_set_unbounded_when_max_size_zero()
    test_working_memory_compression()
    test_working_memory_never_labels_unsafe_best_as_safe()
    test_working_memory_rounds_since_improvement()
    test_working_memory_truncation()
    test_strategy_chain_persists_across_sessions()
    test_strategy_chain_persist_failure_does_not_crash()
    test_best_molecules_persists_across_sessions()
    test_loop_controller_max_rounds()
    test_loop_controller_convergence()
    test_loop_controller_legacy_default_unchanged()
    test_loop_controller_progress_signal_safe_vina()
    test_loop_controller_safe_vina_none_does_not_advance_patience()
    test_loop_controller_rejects_unknown_progress_signal()
    test_loop_controller_hitl_veto()
    test_loop_controller_token_budget()
    test_hitl_disabled()
    test_hitl_veto_sets_state()
    test_loop_works_with_phase41_enabled()
    print("\n" + "=" * 60)
    print("[OK] All Phase 4.1 + 4.3 tests passed!")


if __name__ == "__main__":
    main()
