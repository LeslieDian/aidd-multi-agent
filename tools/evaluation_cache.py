"""Protocol-scoped persistent cache for deterministic molecule evaluations."""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path

from rdkit import Chem

from tools.provenance import digest


_EVALUATION_FIELDS = (
    "validate",
    "admet",
    "dock",
    "scaffold",
    "composite_score",
    "evaluation_status",
    "property_score",
    "score_components",
    "safety_gate_pass",
    "funnel",
    "docking_cache",
    "protocol_id",
    "provenance",
)
_CACHEABLE_STATUSES = {"complete", "screening_only", "screened_out"}


class EvaluationCache:
    """Store one JSON entry per canonical molecule under one protocol ID."""

    def __init__(self, root: str | Path, protocol_id: str, enabled: bool = True):
        self.root = Path(root)
        self.protocol_id = str(protocol_id)
        self.enabled = bool(enabled)
        # Keep paths below the Windows legacy MAX_PATH limit. The full IDs are
        # still stored and verified inside every entry.
        self.directory = self.root / self.protocol_id[:24]

    @staticmethod
    def canonicalize(smiles: str) -> str | None:
        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return None
        return Chem.MolToSmiles(mol)

    def cache_key(self, canonical_smiles: str) -> str:
        return digest({
            "protocol_id": self.protocol_id,
            "canonical_smiles": canonical_smiles,
        })

    def entry_path(self, canonical_smiles: str) -> Path:
        return self.directory / f"{self.cache_key(canonical_smiles)[:40]}.json"

    def get(self, smiles: str) -> dict | None:
        """Return a validated cache payload, or None on any stale/corrupt entry."""
        if not self.enabled:
            return None
        canonical = self.canonicalize(smiles)
        if canonical is None:
            return None
        path = self.entry_path(canonical)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if payload.get("protocol_id") != self.protocol_id:
            return None
        if payload.get("canonical_smiles") != canonical:
            return None
        evaluation = payload.get("evaluation")
        if not isinstance(evaluation, dict):
            return None
        if evaluation.get("evaluation_status") not in _CACHEABLE_STATUSES:
            return None
        if evaluation.get("protocol_id") != self.protocol_id:
            return None

        # A complete docking result is reusable only while its original
        # artifacts remain available. If they were deleted, recompute it.
        dock = evaluation.get("dock") or {}
        reusable_dock = dock
        if evaluation.get("evaluation_status") == "screened_out":
            reusable_dock = ((evaluation.get("funnel") or {}).get("screening_dock") or {})
        if reusable_dock.get("valid"):
            artifact_dir = ((reusable_dock.get("artifacts") or {}).get("directory"))
            if not artifact_dir or not (Path(artifact_dir) / "result.json").is_file():
                return None
        payload["cache_entry"] = str(path.resolve())
        return payload

    def materialize(self, candidate: dict, payload: dict) -> dict:
        """Combine current proposal identity with cached scientific results."""
        result = dict(candidate)
        result.update(copy.deepcopy(payload["evaluation"]))
        result["evaluation_cache"] = {
            "hit": True,
            "protocol_id": self.protocol_id,
            "canonical_smiles": payload["canonical_smiles"],
            "cache_key": payload["cache_key"],
            "cache_entry": payload["cache_entry"],
            "cached_at": payload.get("cached_at"),
            "source": copy.deepcopy(payload.get("source") or {}),
        }
        return result

    def put(self, evaluated: dict) -> dict:
        """Persist a successful evaluation and return cache metadata."""
        status = evaluated.get("evaluation_status")
        canonical = self.canonicalize(evaluated.get("smiles", ""))
        if not self.enabled or status not in _CACHEABLE_STATUSES or canonical is None:
            return {"stored": False, "reason": "not_cacheable"}
        if evaluated.get("protocol_id") != self.protocol_id:
            return {"stored": False, "reason": "protocol_mismatch"}

        path = self.entry_path(canonical)
        existing = self.get(canonical)
        if existing is not None:
            return {
                "stored": False,
                "reason": "already_cached",
                "canonical_smiles": canonical,
                "cache_key": existing["cache_key"],
                "cache_entry": existing["cache_entry"],
                "source": copy.deepcopy(existing.get("source") or {}),
            }

        source = {
            "run_id": evaluated.get("run_id"),
            "candidate_id": evaluated.get("candidate_id"),
            "round": evaluated.get("round"),
            "provider": evaluated.get("provider"),
            "model": evaluated.get("model"),
        }
        payload = {
            "schema_version": 1,
            "protocol_id": self.protocol_id,
            "canonical_smiles": canonical,
            "cache_key": self.cache_key(canonical),
            "cached_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "evaluation": {
                key: copy.deepcopy(evaluated.get(key)) for key in _EVALUATION_FIELDS
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".tmp-{uuid.uuid4().hex}.json")
        try:
            temp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.replace(path)
        finally:
            if temp.exists():
                temp.unlink()
        return {
            "stored": True,
            "canonical_smiles": canonical,
            "cache_key": payload["cache_key"],
            "cache_entry": str(path.resolve()),
            "source": source,
        }
