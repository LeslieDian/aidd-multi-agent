import json
from pathlib import Path

from tools.docking_cache import DockingCache


def test_docking_cache_reuses_artifact_across_scoring_layers(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "result.json").write_text("{}", encoding="utf-8")
    dock = {
        "valid": True,
        "score": -8.1,
        "artifacts": {"directory": str(artifact.resolve())},
    }
    cache = DockingCache(tmp_path / "cache", "dock-protocol")
    stored = cache.put("CCO", dock, source={"run_id": "first"})
    assert stored["stored"] is True
    payload = cache.get("OCC")
    assert payload is not None
    assert payload["dock"]["score"] == -8.1
    assert payload["source"]["run_id"] == "first"


def test_docking_cache_rejects_missing_artifact(tmp_path):
    missing = tmp_path / "missing"
    cache = DockingCache(tmp_path / "cache", "dock-protocol")
    cache.put("CCO", {
        "valid": True, "score": -8.1,
        "artifacts": {"directory": str(missing.resolve())},
    })
    # The entry can be written for audit, but cannot be reused without artifacts.
    assert cache.get("CCO") is None
