"""Persistent cache for docking results separated from downstream scoring."""
from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path

from rdkit import Chem

from tools.provenance import digest


class DockingCache:
    def __init__(self, root: str | Path, protocol_id: str, enabled: bool = True):
        self.root = Path(root)
        self.protocol_id = str(protocol_id)
        self.enabled = bool(enabled)
        self.directory = self.root / self.protocol_id[:24]

    @staticmethod
    def canonicalize(smiles: str) -> str | None:
        mol = Chem.MolFromSmiles(smiles)
        return Chem.MolToSmiles(mol) if mol is not None else None

    def cache_key(self, canonical_smiles: str) -> str:
        return digest({
            "docking_protocol_id": self.protocol_id,
            "canonical_smiles": canonical_smiles,
        })

    def entry_path(self, canonical_smiles: str) -> Path:
        return self.directory / f"{self.cache_key(canonical_smiles)[:40]}.json"

    def get(self, smiles: str) -> dict | None:
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
        if payload.get("docking_protocol_id") != self.protocol_id:
            return None
        if payload.get("canonical_smiles") != canonical:
            return None
        dock = payload.get("dock")
        if not isinstance(dock, dict) or not dock.get("valid"):
            return None
        artifact_dir = ((dock.get("artifacts") or {}).get("directory"))
        if not artifact_dir or not (Path(artifact_dir) / "result.json").is_file():
            return None
        payload["cache_entry"] = str(path.resolve())
        return payload

    def put(self, smiles: str, dock: dict, source: dict | None = None) -> dict:
        canonical = self.canonicalize(smiles)
        if not self.enabled or canonical is None or not dock.get("valid"):
            return {"stored": False, "reason": "not_cacheable"}
        existing = self.get(canonical)
        if existing is not None:
            return {
                "stored": False,
                "reason": "already_cached",
                "cache_entry": existing["cache_entry"],
                "source": copy.deepcopy(existing.get("source") or {}),
            }
        path = self.entry_path(canonical)
        payload = {
            "schema_version": 1,
            "docking_protocol_id": self.protocol_id,
            "canonical_smiles": canonical,
            "cache_key": self.cache_key(canonical),
            "cached_at": datetime.now().isoformat(timespec="seconds"),
            "source": copy.deepcopy(source or {}),
            "dock": copy.deepcopy(dock),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".tmp-{uuid.uuid4().hex}.json")
        try:
            temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temp.replace(path)
        finally:
            if temp.exists():
                temp.unlink()
        return {
            "stored": True,
            "cache_entry": str(path.resolve()),
            "source": copy.deepcopy(payload["source"]),
        }

    @staticmethod
    def materialize(payload: dict) -> dict:
        return copy.deepcopy(payload["dock"])
