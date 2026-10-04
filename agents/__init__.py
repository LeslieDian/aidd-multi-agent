"""agents package - Phase 2/3 + Phase 4 modular memory + Phase 4.6 multi-agent."""
from .llm import LLMClient, MockLLMClient, get_client
from .generator import generate_candidates, generate_with_provider
from .evaluator import evaluate_candidates, summarize_round
from .judge import judge_round
# Phase 4.1 modules
from .loop_controller import LoopController, LoopState, LoopConfig
from .failed_set import FailedLigandSet
from .working_memory import WorkingMemory
from .hitl import HITLCheckpoint
# Phase 4.5 modular memory
from .rule_memory import RuleStore
from .agent_metrics import compute_agent_metrics
# Phase 4.6 multi-agent coordination
from .multi_agent import (
    AggregatedCandidate,
    JudgeVerdict,
    MultiAgentConfigError,
    aggregate_candidates,
    combine_judge_votes,
    generators_are_heterogeneous,
    should_enter_debate,
    validate_multi_agent_config,
)
from .router import RoundFingerprint, route, router_enabled
from .debate import (
    DebateOutcome,
    DebateTurn,
    extract_evidence_ids,
    run_debate,
    should_terminate_debate,
    validate_critic_turn,
)
from .prompts import load as load_prompt, render as render_prompt

__all__ = [
    "LLMClient", "MockLLMClient", "get_client",
    "generate_candidates", "generate_with_provider",
    "evaluate_candidates", "summarize_round",
    "judge_round",
    "LoopController", "LoopState", "LoopConfig",
    "FailedLigandSet", "WorkingMemory", "HITLCheckpoint",
    "RuleStore", "compute_agent_metrics",
    "AggregatedCandidate", "JudgeVerdict", "MultiAgentConfigError",
    "aggregate_candidates", "combine_judge_votes",
    "generators_are_heterogeneous", "should_enter_debate",
    "validate_multi_agent_config",
    "RoundFingerprint", "route", "router_enabled",
    "DebateOutcome", "DebateTurn",
    "extract_evidence_ids", "run_debate",
    "should_terminate_debate", "validate_critic_turn",
    "load_prompt", "render_prompt",
]
__version__ = "0.4.7"