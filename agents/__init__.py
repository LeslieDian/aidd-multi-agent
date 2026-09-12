"""agents package - Phase 2/3 implementations live here."""
from .llm import LLMClient, MockLLMClient, get_client

__all__ = ["LLMClient", "MockLLMClient", "get_client"]
__version__ = "0.1.0"