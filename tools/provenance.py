"""Small, JSON-safe provenance helpers; never capture environment secrets."""
import hashlib
import json
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path


def file_hash(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def versions():
    result = {}
    for name in ('rdkit', 'meeko', 'numpy', 'openai'):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def docking_protocol(target, scoring):
    """Identity for reusable docking physics, independent of later scoring."""
    from tools.dock_score import resolve_vina_binary
    try:
        binary = resolve_vina_binary()
    except FileNotFoundError:
        binary = None
    root = Path(__file__).resolve().parents[1]
    version_info = versions()
    return {
        "target": target,
        "vina": (scoring or {}).get("vina", {}),
        "receptor_sha256": file_hash(target["receptor_pdbqt"]),
        "receptor_audit_sha256": file_hash(
            Path(target["receptor_pdbqt"]).with_suffix('.audit.json')
        ),
        "vina_binary_sha256": file_hash(binary) if binary else None,
        "versions": {name: version_info.get(name) for name in ("rdkit", "meeko")},
        "implementation_sha256": {
            "tools/dock_score.py": file_hash(root / "tools/dock_score.py"),
        },
    }


def docking_protocol_from_evaluation(protocol):
    """Derive the docking identity from a stored evaluation protocol."""
    implementation = protocol.get("implementation_sha256") or {}
    version_info = protocol.get("versions") or {}
    return {
        "target": protocol.get("target"),
        "vina": (protocol.get("scoring") or {}).get("vina", {}),
        "receptor_sha256": protocol.get("receptor_sha256"),
        "receptor_audit_sha256": protocol.get("receptor_audit_sha256"),
        "vina_binary_sha256": protocol.get("vina_binary_sha256"),
        "versions": {name: version_info.get(name) for name in ("rdkit", "meeko")},
        "implementation_sha256": {
            "tools/dock_score.py": implementation.get("tools/dock_score.py"),
        },
    }


def evaluation_protocol(target, scoring, dock_enabled):
    """One protocol identity used by run manifests, candidates and memory buckets."""
    binary = None
    if dock_enabled:
        from tools.dock_score import resolve_vina_binary
        try:
            binary = resolve_vina_binary()
        except FileNotFoundError:
            pass
    root = Path(__file__).resolve().parents[1]
    result = {
        "target": target, "scoring": scoring, "dock_enabled": dock_enabled,
        "receptor_sha256": file_hash(target["receptor_pdbqt"]),
        "receptor_audit_sha256": file_hash(Path(target["receptor_pdbqt"]).with_suffix('.audit.json')),
        "vina_binary_sha256": file_hash(binary) if binary else None,
        "versions": versions(), "score_version": "multi-objective-v3.1",
        "implementation_sha256": {name: file_hash(root / name) for name in (
            'agents/evaluator.py', 'tools/dock_score.py', 'tools/validate_mol.py',
            'tools/admet_score.py', 'tools/sascorer.py', 'tools/fpscores.pkl.gz')},
    }
    if dock_enabled:
        result["docking_protocol_id"] = digest(docking_protocol(target, scoring))
    return result
