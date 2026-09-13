"""agents/llm.py - OpenAI-compatible LLM client for DeepSeek + MiniMax.

Both providers expose an OpenAI-compatible chat completions endpoint, so
we share a single client class and just swap base_url / model / extra_body.

API keys are loaded from environment (.env). They are NEVER read from
config.yaml or any tracked file.
"""
from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

# Load .env if present (no-op if python-dotenv is missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class LLMClient:
    """Thin wrapper around OpenAI SDK for OpenAI-compatible providers."""

    def __init__(self, provider_config: dict, api_key: str | None = None):
        self.cfg = provider_config
        env_var = provider_config["api_key_env"]
        self.api_key = api_key or os.getenv(env_var)
        if not self.api_key:
            raise EnvironmentError(
                f"Missing API key: set {env_var} in .env (see docs/SECURITY.md)"
            )
        self.client = OpenAI(
            base_url=provider_config["base_url"],
            api_key=self.api_key,
        )

    @property
    def model(self) -> str:
        return self.cfg["model"]

    def chat(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **extra,
    ) -> str:
        """Call chat completion, return raw text content."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict[str, Any] = {
            "model": self.cfg["model"],
            "messages": messages,
            "temperature": temperature if temperature is not None else self.cfg.get("temperature", 0.7),
            "max_tokens": max_tokens or self.cfg.get("max_tokens", 2048),
        }
        if json_mode:
            # OpenAI-compatible JSON mode
            kwargs["response_format"] = {"type": "json_object"}
        # Provider-specific extra body (e.g., MiniMax thinking control)
        if "extra_body" in self.cfg:
            kwargs["extra_body"] = {**self.cfg["extra_body"], **extra.pop("extra_body", {})}
        if extra:
            kwargs.update(extra)

        response = self.client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    def chat_json(self, system: str, user: str, **kwargs) -> Any:
        """Call chat and parse JSON response."""
        text = self.chat(system, user, json_mode=True, **kwargs)
        # Robust JSON parse: strip code fences if present
        text = text.strip()
        if text.startswith("```"):
            # strip first and last ```
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines)
        return json.loads(text)


# --------- Mock client for offline testing ---------

class MockLLMClient:
    """Deterministic mock used when API keys are missing or in CI."""

    def __init__(self, provider_config: dict | None = None):
        self.cfg = provider_config or {"model": "mock"}

    @property
    def model(self) -> str:
        return self.cfg["model"]

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs) -> str:
        if json_mode:
            # Phase 4.2: include reflection/confidence/adopted_count so mock
            # behaves like a real Phase 4.2 judge (for tests).
            # Detect judge vs generator from system prompt keywords.
            if "experienced medicinal chemist leading" in system:
                # Judge-style response
                return json.dumps({
                    "focus": "Add a morpholine to improve aqueous solubility and Vina binding.",
                    "best_index": 0,
                    "weakness": "All candidates lack a solubilizing group on the western aryl ring.",
                    "expected_change": "logP -0.5",
                    "reasoning": "Adding morpholine should drop logP and improve solubility per erlotinib SAR.",
                    "reflection": "Previous round added morpholine and Vina improved by 0.3, so keep that direction.",
                    "confidence": 0.7,
                    "adopted_count": 2,
                })
            # Default: generator-style response (SMILES list)
            return json.dumps({
                "smiles_list": [
                    "CC(=O)Oc1ccccc1C(=O)O",   # aspirin
                    "CC(C)Cc1ccc(C(C)C(=O)O)cc1",  # ibuprofen
                    "Cn1cnc2c1c(=O)n(C)c(=O)n2C",  # caffeine
                    "OC(=O)C1CCCCC1",            # cyclohexanecarboxylic acid
                    "CCO",                       # ethanol
                ],
                "rationale": "Mock: 5 stable drug-like molecules.",
            })
        return "Mock response for: " + user[:60]

    def chat_json(self, system: str, user: str, **kwargs) -> Any:
        return json.loads(self.chat(system, user, json_mode=True, **kwargs))


def get_client(provider_name: str, config: dict, mock: bool = False) -> LLMClient | MockLLMClient:
    """Factory: return LLMClient or MockLLMClient.

    If `mock=True` or the API key env var is missing, return MockLLMClient.
    """
    providers = config.get("llm", {}).get("providers", {})
    if provider_name not in providers:
        raise KeyError(f"Unknown provider: {provider_name}. Available: {list(providers)}")
    provider_cfg = providers[provider_name]

    if mock or not os.getenv(provider_cfg["api_key_env"]):
        return MockLLMClient(provider_cfg)
    return LLMClient(provider_cfg)