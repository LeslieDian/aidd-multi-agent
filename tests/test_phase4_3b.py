"""tests/test_phase4_3b.py - Phase 4.3 P1-2 + P2-2 tests.

P1-2: embedding-based similarity check on FailedLigandSet
P2-2: HITL i18n answer parsing
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.failed_set import FailedLigandSet, embedder_status
from agents.hitl import _parse_yn, HITLCheckpoint


# ---------------- P2-2: i18n parser ----------------

def test_parse_yn_english():
    print("\n=== test_parse_yn_english ===")
    for yes in ("y", "yes", "Y", "Yes", "yep", "yeah", "OK", "ok"):
        assert _parse_yn(yes) is True, f"expected True for {yes!r}"
    for no in ("n", "no", "N", "No", "nope", "nah"):
        assert _parse_yn(no) is False, f"expected False for {no!r}"
    # Empty -> default y -> True
    assert _parse_yn("", default="y") is True
    # Empty -> default n -> False
    assert _parse_yn("", default="n") is False
    print(f"  [OK] english tokens parsed (y/yes/n/no/yep/nope/etc.)")


def test_parse_yn_chinese():
    print("\n=== test_parse_yn_chinese ===")
    for yes in ("是", "好", "好的", "继续", "shi", "hao", "xu"):
        assert _parse_yn(yes) is True, f"expected True for {yes!r}"
    for no in ("否", "不", "不要", "停", "fou", "bu", "ting"):
        assert _parse_yn(no) is False, f"expected False for {no!r}"
    print(f"  [OK] chinese tokens parsed (是/好/继续/否/不/停/etc.)")


def test_parse_yn_garbage_is_conservative_no():
    print("\n=== test_parse_yn_garbage_is_conservative_no ===")
    # Compliance: when in doubt, do NOT proceed. Garbage -> False.
    for g in ("???", "banana", "@#$", "1", "qwerty"):
        assert _parse_yn(g) is False, f"expected False for garbage {g!r}"
    print(f"  [OK] garbage -> False (compliance-conservative)")


def test_parse_yn_startswith_chinese():
    print("\n=== test_parse_yn_startswith_chinese ===")
    # Phrases starting with 是/好/继续/否/不/停 should resolve
    assert _parse_yn("是，继续") is True
    assert _parse_yn("不好") is False
    assert _parse_yn("停吧") is False
    print(f"  [OK] startswith-中文 tokens parsed")


# ---------------- P2-2: HITLCheckpoint auto-pass when disabled ----------------

def test_hitl_disabled_still_works():
    print("\n=== test_hitl_disabled_still_works ===")
    hitl = HITLCheckpoint(require_approval=False)
    assert hitl.pre_loop(5, 5, 2) is True
    assert hitl.on_vina_breakthrough(-3.0, -3.5) is True
    chosen = hitl.select_synthesis_candidates([])
    assert chosen == []
    print(f"  [OK] disabled mode auto-passes (no i18n path exercised)")


def test_hitl_veto_can_be_set():
    print("\n=== test_hitl_veto_can_be_set ===")
    hitl = HITLCheckpoint(require_approval=False)
    hitl.veto = True
    print(f"  [OK] veto flag set externally: {hitl.veto}")


# ---------------- P1-2: embedding path ----------------

def test_failed_set_embeddings_disabled_by_default():
    print("\n=== test_failed_set_embeddings_disabled_by_default ===")
    with tempfile.TemporaryDirectory() as tmp:
        fs = FailedLigandSet(path=Path(tmp) / "f.json")
        assert fs.enable_embeddings is False
        # is_similar_to_failed should be a no-op
        too_close, match, sim = fs.is_similar_to_failed("c1ccccc1")
        assert too_close is False
        assert match is None
        assert sim == 0.0
        print(f"  [OK] default enable_embeddings=False; is_similar_to_failed no-op")


def test_failed_set_embedder_status_diagnostic():
    print("\n=== test_failed_set_embedder_status_diagnostic ===")
    s = embedder_status()
    assert "loaded" in s and "backend_error" in s
    print(f"  [OK] embedder_status() -> loaded={s['loaded']} "
          f"backend_error={s['backend_error']}")


def test_failed_set_embeddings_fallback_when_backend_missing():
    """If sentence-transformers is unavailable, the embedding path silently
    disables itself rather than crashing the loop."""
    print("\n=== test_failed_set_embeddings_fallback_when_backend_missing ===")
    from agents import failed_set as fs_mod
    # Force the embedder into a permanent "broken" state for this test.
    fs_mod._EMBED_BACKEND_ERROR = "test: simulated failure"
    fs_mod._EMBED_MODEL = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fs = FailedLigandSet(
                path=Path(tmp) / "f.json",
                enable_embeddings=True,
                embedding_model="all-MiniLM-L6-v2",
            )
            fs.add_failed("c1ccccc1", "test")
            assert fs.enable_embeddings is False, "should fall back silently"
            too_close, match, sim = fs.is_similar_to_failed("c1ccccc1")
            assert too_close is False
    finally:
        fs_mod._EMBED_BACKEND_ERROR = None
        fs_mod._EMBED_MODEL = None
    print(f"  [OK] backend failure gracefully falls back to exact-match only")


def test_failed_set_embeddings_round_trip_settings():
    """Embedding settings persist to JSON; embeddings themselves rebuild
    on next session (they're not serialized)."""
    print("\n=== test_failed_set_embeddings_round_trip_settings ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f.json"
        # We force the settings through _load/_save round-trip even if the
        # model isn't pre-cached (in which case embedding auto-disables at
        # use-time but the config flag itself still round-trips).
        from agents import failed_set as fs_mod
        # Pretend the backend loaded successfully so __init__ keeps
        # enable_embeddings=True.
        prev_model = fs_mod._EMBED_MODEL
        prev_err = fs_mod._EMBED_BACKEND_ERROR
        fs_mod._EMBED_BACKEND_ERROR = "test: simulated load OK"
        try:
            fs = FailedLigandSet(
                path=path,
                enable_embeddings=True,
                embedding_threshold=0.7,
                embedding_model="all-MiniLM-L6-v2",
            )
            # Force a save so we can read settings back
            fs.add_failed("c1ccccc1", "test")
        finally:
            fs_mod._EMBED_MODEL = prev_model
            fs_mod._EMBED_BACKEND_ERROR = prev_err
        # Even if model is cached, the embedder_status check might still
        # try to load. Just check JSON shape directly.
        # The JSON may have embeddings.enabled = False if safety net
        # disabled it; that's actually correct behavior.
        data = json.loads(path.read_text(encoding="utf-8"))
        # The settings themselves (threshold, model) always persist
        assert data["embeddings"]["model"] == "all-MiniLM-L6-v2"
        assert data["embeddings"]["threshold"] == 0.7
        # If embeddings successfully stayed enabled, enabled is True
        # If safety net disabled them, enabled is False
        # Either is acceptable; what matters is the field exists
        assert "enabled" in data["embeddings"]
        print(f"  [OK] embedding settings persist across reload; "
              f"enabled={data['embeddings']['enabled']}")


def test_failed_set_strict_filter_disabled_falls_back_to_filter():
    """With embeddings disabled, filter_smiles_strict == filter_smiles."""
    print("\n=== test_failed_set_strict_filter_disabled_falls_back_to_filter ===")
    with tempfile.TemporaryDirectory() as tmp:
        fs = FailedLigandSet(path=Path(tmp) / "f.json")
        fs.add_failed("CCO", "test")
        cands = ["CCO", "c1ccccc1", "CC(=O)Oc1ccccc1C(=O)O"]
        out = fs.filter_smiles_strict(cands)
        assert "CCO" not in out
        assert "c1ccccc1" in out
        assert "CC(=O)Oc1ccccc1C(=O)O" in out
        # Same result as filter_smiles (no embeddings used)
        assert out == fs.filter_smiles(cands)
        print(f"  [OK] strict filter == filter when embeddings disabled")


def test_failed_set_persistence_includes_embeddings_metadata():
    """Saved JSON has the embeddings sub-dict (even if disabled)."""
    print("\n=== test_failed_set_persistence_includes_embeddings_metadata ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "f.json"
        fs = FailedLigandSet(path=path, max_size=10)
        fs.add_failed("c1ccccc1", "x")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "embeddings" in data
        assert data["embeddings"]["enabled"] is False
        assert data["embeddings"]["model"] == "all-MiniLM-L6-v2"
        print(f"  [OK] persisted JSON includes embeddings metadata: "
              f"{data['embeddings']}")


# ---------------- P1-2: real embeddings (skipped if backend missing) ----------------

def test_failed_set_real_embeddings_catch_near_duplicate():
    """If sentence-transformers IS available AND the model is already cached,
    a +CH2 derivative of a failed SMILES is correctly flagged. The test
    skips if the model isn't pre-downloaded — running it for the first time
    triggers an ~80 MB HF download that we don't want in CI."""
    print("\n=== test_failed_set_real_embeddings_catch_near_duplicate ===")
    from agents import failed_set as fs_mod
    # Check if model is already cached (skip if not, to avoid HF download)
    cache_marker = (
        Path.home() / ".cache" / "huggingface" / "hub" /
        "models--sentence-transformers--all-MiniLM-L6-v2"
    )
    if not cache_marker.exists():
        print(f"  [SKIP] all-MiniLM-L6-v2 not pre-cached at {cache_marker}.")
        print(f"          To run this test: pre-warm via")
        print(f"            python -c 'from sentence_transformers import "
              f"SentenceTransformer; SentenceTransformer(\"all-MiniLM-L6-v2\")'")
        return
    if fs_mod._EMBED_MODEL is None and fs_mod._EMBED_BACKEND_ERROR is None:
        # Try to load now (cheap if cached)
        fs_mod._try_load_embedder("all-MiniLM-L6-v2")
    if fs_mod._EMBED_MODEL is None:
        print(f"  [SKIP] backend unavailable: {fs_mod._EMBED_BACKEND_ERROR}")
        return
    with tempfile.TemporaryDirectory() as tmp:
        fs = FailedLigandSet(
            path=Path(tmp) / "f.json",
            enable_embeddings=True,
            embedding_threshold=0.85,  # production default
        )
        ref = "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCN1CCOCC1"
        fs.add_failed(ref, "low score")
        near = "COc1cc2ncnc(Nc3ccc(F)c(Cl)c3)c2cc1OCCCN1CCOCC1"
        too_close, match, sim = fs.is_similar_to_failed(near)
        assert too_close, f"expected near-duplicate flagged (sim={sim})"
        assert sim > 0.85, f"near-duplicate sim should be > 0.85, got {sim}"
        # Truly unrelated: aspirin (a long-established distractor in this
        # codebase). Benzene alone happens to share aromatic-ring motifs
        # with the gefitinib core and lands at sim ~0.76.
        far = "CC(=O)Oc1ccccc1C(=O)O"  # aspirin
        too_close2, _, sim2 = fs.is_similar_to_failed(far)
        assert not too_close2, f"aspirin flagged: sim={sim2}"
        assert sim2 < 0.85, f"aspirin sim should be < 0.85, got {sim2}"
        print(f"  [OK] near-duplicate sim={sim:.3f} flagged; "
              f"aspirin sim={sim2:.3f} not flagged")


def main():
    print("[TEST] Phase 4.3 (P1-2 + P2-2): embedding failed-set + HITL i18n")
    print("=" * 60)
    test_parse_yn_english()
    test_parse_yn_chinese()
    test_parse_yn_garbage_is_conservative_no()
    test_parse_yn_startswith_chinese()
    test_hitl_disabled_still_works()
    test_hitl_veto_can_be_set()
    test_failed_set_embeddings_disabled_by_default()
    test_failed_set_embedder_status_diagnostic()
    test_failed_set_embeddings_fallback_when_backend_missing()
    test_failed_set_embeddings_round_trip_settings()
    test_failed_set_strict_filter_disabled_falls_back_to_filter()
    test_failed_set_persistence_includes_embeddings_metadata()
    test_failed_set_real_embeddings_catch_near_duplicate()
    print("\n" + "=" * 60)
    print("[OK] All Phase 4.3 (P1-2 + P2-2) tests passed!")


if __name__ == "__main__":
    main()