"""agents/failed_set.py - Persistent set of failed ligand SMILES (Phase 4.1).

Naive set-based dedup with JSON persistence. Zero embedding cost.
Catches the highest-value form of agent memory: "don't waste tokens
re-proposing molecules that already failed this session or a prior one."
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from rdkit import Chem
from rdkit import RDLogger

RDLogger.DisableLog("rdApp.*")


class FailedLigandSet:
    """Set of canonical SMILES that failed (composite<V_thresh OR vina>V_thresh).

    Persists to JSON so knowledge survives across sessions.
    """

    DEFAULT_PATH = Path("memory/failed_ligands.json")

    def __init__(
        self,
        path: Path | str | None = None,
        threshold_composite: float = 0.5,
        threshold_vina: float = -2.5,
    ):
        self.path = Path(path) if path else self.DEFAULT_PATH
        self.threshold_composite = threshold_composite
        self.threshold_vina = threshold_vina
        self.failed: set[str] = set()
        self.reasons: dict[str, str] = {}
        self._load()

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
        self.failed.add(canon)
        self.reasons[canon] = reason
        self._save()
        return True

    def is_failed(self, smiles: str) -> bool:
        canon = self.canonicalize(smiles)
        return canon is not None and canon in self.failed

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

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"failed": sorted(self.failed),
                 "reasons": self.reasons,
                 "thresholds": {
                     "composite": self.threshold_composite,
                     "vina": self.threshold_vina,
                 }},
                indent=2,
            ),
            encoding="utf-8",
        )

    def clear(self) -> None:
        self.failed.clear()
        self.reasons.clear()
        self._save()