"""tools 鈥?Phase 1 鐨?4 涓嫭绔嬭瘎鍒嗗伐鍏?

姣忎釜宸ュ叿閮芥槸**绾嚱鏁?+ 鏍囧噯鍖?JSON 杈撳嚭**锛屽彲浠ヨ Agent 鐩存帴浼犲弬璋冪敤銆?
"""
from .validate_mol import validate_smiles, validate_batch, lipinski_pass
from .admet_score import estimate_admet, admet_batch
from .diversity import get_scaffold, scaffold_diversity, batch_scaffolds
from .dock_score import dock_smiles, dock_batch, is_vina_available
# Phase 4.5 (2026-09-27, Priority A-3): selection operator library
from .mutate import (
    nearest_neighbors,
    nearest_neighbors_with_meta,
    brics_reassemble,
    atom_substitution,
    terminal_swap,
    mutate,
    format_parents_block,
    offline_validation,
    DEFAULT_TERMINALS,
    PHARMA_SUBSTITUTIONS,
)

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
    # mutate (selection operators, Phase 4.5)
    "nearest_neighbors",
    "nearest_neighbors_with_meta",
    "brics_reassemble",
    "atom_substitution",
    "terminal_swap",
    "mutate",
    "format_parents_block",
    "offline_validation",
    "DEFAULT_TERMINALS",
    "PHARMA_SUBSTITUTIONS",
]

__version__ = "0.4.5"
