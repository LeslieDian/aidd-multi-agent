"""agents package - Phase 2/3 implementations live here."""
from .llm import LLMClient, MockLLMClient, get_client
from .generator import generate_candidates, generate_with_provider
from .evaluator import evaluate_candidates, summarize_round
from .judge import judge_round

__all__ = [
    "LLMClient", "MockLLMClient", "get_client",
    "generate_candidates", "generate_with_provider",
    "evaluate_candidates", "summarize_round",
    "judge_round",
]
__version__ = "0.2.0"