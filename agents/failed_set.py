"""agents/failed_set.py - Persistent set of failed ligand SMILES (Phase 4.1).

Naive set-based dedup with JSON persistence. Zero embedding cost.
Catches the highest-value form of agent memory: "don't waste tokens
re-proposing molecules that already failed this session or a prior one."

Phase 4.3 (P1-2): optional embedding-based similarity check. When
`enable_embeddings=True`, the set also encodes every SMILES with
sentence-transformers (default all-MiniLM-L6-v2, 384-dim) and treats any
new candidate whose top-1 cosine similarity to the failed set exceeds
`embedding_threshold` (default 0.85) as failed. This catches structurally
near-duplicates the exact-match path misses (e.g. an extra CH2 on a
known-bad scaffold). Lazy model load — only downloads/loads the model
on the first similarity query. Falls back silently to exact-match if
sentence-transformers or torch is unavailable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from rdkit import Chem
from rdkit import RDLogger

RDLogger.DisableLog("rdApp.*")

# Lazy-loaded globals (do NOT import sentence_transformers at module level —
# tests + non-P1-2 code paths should not pay the import cost).
_EMBED_MODEL = None
_EMBED_BACKEND_ERROR: Optional[str] = None
_EMBED_DEVICE: Optional[str] = None


def _is_model_cached(model_name: str) -> bool:
    """Cheap filesystem check: is the HF model already on disk?

    Avoids blocking downloads in __init__ and tests. Real downloads
    happen only at use time (in `is_similar_to_failed` / `add_failed`),
    and can be opted-out via env var AIDD_EMBED_AUTO_DOWNLOAD=0.
    """
    # Default HF cache location
    cache_root = Path.home() / ".cache" / "huggingface" / "hub"
    if not cache_root.exists():
        return False
    # Sentence-transformers normalizes "all-MiniLM-L6-v2" ->
    # "models--sentence-transformers--all-MiniLM-L6-v2"
    folder_name = "models--sentence-transformers--" + model_name
    return (cache_root / folder_name).exists()


def _resolve_embedding_device(requested: str | None = "auto") -> str:
    """Prefer CUDA when available while keeping CPU-only deployments portable."""
    requested = str(requested or "auto").lower()
    if requested not in {"auto", "cpu", "cuda"}:
        raise ValueError("embedding device must be auto, cpu, or cuda")
    if requested == "cpu":
        return "cpu"
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _try_load_embedder(
    model_name: str,
    allow_download: bool | None = None,
    device: str | None = "auto",
):
    """Lazy-load sentence-transformers. Returns the model or None.

    By default this does NOT auto-download. Set
    AIDD_EMBED_AUTO_DOWNLOAD=1 in env to opt in (slow on first run;
    ~80 MB).
    """
    global _EMBED_MODEL, _EMBED_BACKEND_ERROR, _EMBED_DEVICE
    if _EMBED_MODEL is not None:
        return _EMBED_MODEL
    if _EMBED_BACKEND_ERROR is not None:
        return None
    if allow_download is None:
        allow_download = os.environ.get("AIDD_EMBED_AUTO_DOWNLOAD", "0") == "1"
    if not allow_download and not _is_model_cached(model_name):
        _EMBED_BACKEND_ERROR = (
            f"model {model_name!r} not pre-cached; set "
            f"AIDD_EMBED_AUTO_DOWNLOAD=1 to allow download"
        )
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as e:
        _EMBED_BACKEND_ERROR = f"sentence-transformers import failed: {e}"
        return None
    try:
        _EMBED_DEVICE = _resolve_embedding_device(device)
        try:
            _EMBED_MODEL = SentenceTransformer(model_name, device=_EMBED_DEVICE)
        except Exception:
            if str(device or "auto").lower() != "auto" or _EMBED_DEVICE == "cpu":
                raise
            _EMBED_DEVICE = "cpu"
            _EMBED_MODEL = SentenceTransformer(model_name, device="cpu")
    except Exception as e:
        _EMBED_BACKEND_ERROR = f"SentenceTransformer({model_name!r}) failed: {e}"
        return None
    return _EMBED_MODEL


def embedder_status() -> dict:
    """Diagnostic — exposes whether the embedding backend is available."""
    return {
        "model": getattr(_EMBED_MODEL, "model_card_text", None) or "unknown",
        "loaded": _EMBED_MODEL is not None,
        "device": _EMBED_DEVICE,
        "backend_error": _EMBED_BACKEND_ERROR,
    }


class FailedLigandSet:
    """Set of canonical SMILES that failed (composite<V_thresh OR vina>V_thresh).

    Persists to JSON so knowledge survives across sessions.

    Phase 4.3 (P0-2 fix): `max_size` caps the on-disk + in-memory set to
    prevent unbounded growth (the pre-fix bug accumulated one entry per
    failure across sessions indefinitely). When the cap is hit, the
    *oldest* insertion is evicted (LRF / FIFO). Pass `max_size=0` (the
    default) to keep the original unbounded behavior — useful for tests
    that want to assert exact membership.
    """

    DEFAULT_PATH = Path("memory/failed_ligands.json")

    def __init__(
        self,
        path: Path | str | None = None,
        threshold_composite: float = 0.5,
        threshold_vina: float = -2.5,
        max_size: int = 0,
        enable_embeddings: bool = False,
        embedding_threshold: float = 0.85,
        embedding_model: str = "all-MiniLM-L6-v2",
        embedding_device: str = "auto",
    ):
        self.path = Path(path) if path else self.DEFAULT_PATH
        self.threshold_composite = threshold_composite
        self.threshold_vina = threshold_vina
        self.max_size = max(0, int(max_size))
        self.failed: set[str] = set()
        # Reasons are kept in insertion order (Python 3.7+ dict) so the
        # LRF eviction in `add_failed` can pop the oldest entry.
        self.reasons: dict[str, str] = {}
        # Phase 4.3 (P1-2): optional embedding-based similarity. Lazy.
        self.enable_embeddings = bool(enable_embeddings)
        self.embedding_threshold = float(embedding_threshold)
        self.embedding_model = embedding_model
        self.embedding_device = str(embedding_device or "auto")
        self._embeddings: dict[str, list[float]] = {}  # canon_smi -> vec
        self._batching = False
        self._load()
        if self.enable_embeddings:
            self._refresh_embeddings()

    @staticmethod
    def canonicalize(smiles: str) -> str | None:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        try:
            return Chem.MolToSmiles(mol)
        except Exception:
            return None

    def add_failed(self, smiles: str, reason: str) -> bool:
        canon = self.canonicalize(smiles)
        if canon is None:
            return False
        if canon in self.failed:
            return False
        # Phase 4.3 (P0-2): LRF eviction before insertion
        if self.max_size and len(self.reasons) >= self.max_size:
            oldest = next(iter(self.reasons))
            self.reasons.pop(oldest, None)
            self.failed.discard(oldest)
            # Drop the evicted canonical's embedding too — we will refresh
            # below; cheaper to fully refresh than to diff.
        self.failed.add(canon)
        self.reasons[canon] = reason
        # Phase 4.3 (P1-2): keep embedding index in sync. Refresh on every
        # add — at typical sizes (LRF cap=500) this is still cheap (~1s with
        # all-MiniLM-L6-v2 on CPU), and avoids drift.
        if self.enable_embeddings and not self._batching:
            self._refresh_embeddings()
        if not self._batching:
            self._save()
        return True

    def add_failed_many(self, entries: Iterable[tuple[str, str]]) -> int:
        """Add one round of failures with one embedding batch and one save."""
        added = 0
        # Avoid a full-index refresh and disk write for every molecule.
        self._batching = True
        try:
            for smiles, reason in entries:
                added += int(self.add_failed(smiles, reason))
        finally:
            self._batching = False
        if added:
            if self.enable_embeddings:
                self._refresh_embeddings()
            self._save()
        return added

    def is_failed(self, smiles: str) -> bool:
        canon = self.canonicalize(smiles)
        return canon is not None and canon in self.failed

    # ---------- Phase 4.3 (P1-2): embedding-based similarity ----------

    def _refresh_embeddings(self) -> None:
        """Re-encode every failed SMILES. Called once at __init__ and after
        every batch of add_failed() calls.

        If the model isn't pre-cached, embeddings silently disable
        themselves rather than blocking on a download. Callers can
        pre-warm by setting AIDD_EMBED_AUTO_DOWNLOAD=1 (or by passing
        `allow_download=True` to the embedder explicitly).
        """
        if not self.enable_embeddings or not self.failed:
            self._embeddings = {}
            return
        # Cheap check: if model not on disk, don't block on download here.
        if not _is_model_cached(self.embedding_model) and \
                os.environ.get("AIDD_EMBED_AUTO_DOWNLOAD", "0") != "1":
            # Disable embeddings silently; user can opt in via env var.
            self.enable_embeddings = False
            self._embeddings = {}
            return
        model = _try_load_embedder(self.embedding_model, device=self.embedding_device)
        if model is None:
            self.enable_embeddings = False
            self._embeddings = {}
            return
        import numpy as np  # local import; torch/numpy already in env
        smis = sorted(self.failed)
        vecs = model.encode(smis, normalize_embeddings=True, show_progress_bar=False)
        self._embeddings = {s: vecs[i].tolist() for i, s in enumerate(smis)}

    def _ensure_fresh_embeddings(self) -> None:
        """If `self._embeddings` is stale relative to `self.failed`, rebuild.
        Cheap: just compare counts.
        """
        if not self.enable_embeddings:
            return
        if len(self._embeddings) != len(self.failed):
            self._refresh_embeddings()

    def _cosine_top1(self, smiles: str) -> Optional[tuple[str, float]]:
        """Return (matched_failed_smiles, similarity) for the closest entry.

        Returns None if embeddings unavailable or failed set empty.
        """
        if not self.enable_embeddings or not self._embeddings:
            return None
        model = _try_load_embedder(self.embedding_model, device=self.embedding_device)
        if model is None:
            return None
        import numpy as np
        q = model.encode([smiles], normalize_embeddings=True, show_progress_bar=False)[0]
        keys = sorted(self._embeddings.keys())
        if not keys:
            return None
        mat = np.asarray([self._embeddings[k] for k in keys])
        sims = mat @ q  # cos sim (vectors are L2-normalized)
        idx = int(np.argmax(sims))
        return keys[idx], float(sims[idx])

    def is_similar_to_failed(
        self, smiles: str, threshold: Optional[float] = None,
    ) -> tuple[bool, Optional[str], float]:
        """Embedding similarity check. Returns:
            (is_too_similar, matched_failed_smiles_or_None, similarity)

        A True result means "this candidate is structurally too close to a
        previously-failed molecule; skip it". Threshold defaults to
        self.embedding_threshold; pass another value for one-off calls.
        Returns False if embeddings unavailable (use is_failed() first).
        """
        if not self.enable_embeddings:
            return False, None, 0.0
        thr = threshold if threshold is not None else self.embedding_threshold
        canon = self.canonicalize(smiles)
        if canon is None or not self._embeddings:
            return False, None, 0.0
        top = self._cosine_top1(canon)
        if top is None:
            return False, None, 0.0
        match, sim = top
        return sim >= thr, match, sim

    def filter_smiles_strict(self, smiles_list: Iterable[str]) -> list[str]:
        """Strict filter: exclude both exact-match AND embedding-similar."""
        if not self.enable_embeddings:
            return self.filter_smiles(smiles_list)
        self._ensure_fresh_embeddings()
        survivors = []
        for s in smiles_list:
            canon = self.canonicalize(s)
            if canon is None:
                continue
            if canon in self.failed:
                continue
            too_close, _, _ = self.is_similar_to_failed(canon)
            if too_close:
                continue
            survivors.append(s)
        return survivors

    def should_mark_failed(self, composite: float, vina: float | None) -> bool:
        if composite < self.threshold_composite:
            return True
        if vina is not None and vina > self.threshold_vina:
            return True
        return False

    def filter_smiles(self, smiles_list: Iterable[str]) -> list[str]:
        return [s for s in smiles_list if not self.is_failed(s)]

    def format_for_prompt(self, max_show: int = 8) -> str:
        """Compact prompt injection (limit to avoid blowing context)."""
        if not self.failed:
            return ""
        items = sorted(self.failed)[:max_show]
        suffix = f" (and {len(self.failed) - max_show} more)" \
                 if len(self.failed) > max_show else ""
        return (
            "DO NOT propose any of these SMILES (already tried, low score): "
            + ", ".join(items) + suffix
        )

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.failed = set(data.get("failed", []))
        self.reasons = dict(data.get("reasons", {}))
        # Embedding metadata in the file documents how it was produced. Runtime
        # behavior comes from the current constructor/config so an old memory
        # file cannot silently override a new experiment's settings.

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"failed": sorted(self.failed),
                 "reasons": self.reasons,
                 "thresholds": {
                     "composite": self.threshold_composite,
                     "vina": self.threshold_vina,
                 },
                 "embeddings": {
                     "enabled": self.enable_embeddings,
                     "threshold": self.embedding_threshold,
                     "model": self.embedding_model,
                     "device": self.embedding_device,
                     "n_indexed": len(self._embeddings),
                 }},
                indent=2,
            ),
            encoding="utf-8",
        )

    def clear(self) -> None:
        self.failed.clear()
        self.reasons.clear()
        self._save()
