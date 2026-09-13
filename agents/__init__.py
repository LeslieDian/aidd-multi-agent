"""agents package - Phase 2/3 + Phase 4 modular memory."""
from .llm import LLMClient, MockLLMClient, get_client
from .generator import generate_candidates, generate_with_provider
from .evaluator import evaluate_candidates, summarize_round
from .judge import judge_round
# Phase 4.1 modules
from .loop_controller import LoopController, LoopState, LoopConfig
from .failed_set import FailedLigandSet
from .working_memory import WorkingMemory
from .hitl import HITLCheckpoint

__all__ = [
    "LLMClient", "MockLLMClient", "get_client",
    "generate_candidates", "generate_with_provider",
    "evaluate_candidates", "summarize_round",
    "judge_round",
    "LoopController", "LoopState", "LoopConfig",
    "FailedLigandSet", "WorkingMemory", "HITLCheckpoint",
]
__version__ = "0.4.1"