"""tools — Phase 1 的 4 个独立评分工具

每个工具都是**纯函数 + 标准化 JSON 输出**，可以被 Agent 直接传参调用。
"""
from .validate_mol import validate_smiles, validate_batch, lipinski_pass
from .admet_score import estimate_admet, admet_batch
from .diversity import get_scaffold, scaffold_diversity, batch_scaffolds
from .dock_score import dock_smiles, dock_batch, is_vina_available

__all__ = [
    # validate_mol
    "validate_smiles",
    "validate_batch",
    "lipinski_pass",
    # admet
    "estimate_admet",
    "admet_batch",
    # diversity
    "get_scaffold",
    "scaffold_diversity",
    "batch_scaffolds",
    # dock
    "dock_smiles",
    "dock_batch",
    "is_vina_available",
]

__version__ = "0.1.0"