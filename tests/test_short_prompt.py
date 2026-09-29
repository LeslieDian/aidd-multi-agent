"""tests/test_short_prompt.py - Stage 11 short-prompt mode for GLM."""
from __future__ import annotations

import json

import pytest

from agents import generator as gen
import agents.llm as llm_mod


class TestShortPromptTemplate:
    def test_short_system_prompt_exists(self):
        assert hasattr(gen, "SHORT_SYSTEM_PROMPT")
        assert "JSON" in gen.SHORT_SYSTEM_PROMPT
        assert "__N__" in gen.SHORT_SYSTEM_PROMPT

    def test_short_user_prompt_template_exists(self):
        assert hasattr(gen, "SHORT_USER_PROMPT_TEMPLATE")
        assert "__N__" in gen.SHORT_USER_PROMPT_TEMPLATE
        assert "__FOCUS__" in gen.SHORT_USER_PROMPT_TEMPLATE

    def test_short_prompts_are_shorter_than_full(self):
        """Short prompts must be substantially smaller (or GLM reasoning
        will burn all of max_tokens).
        """
        assert len(gen.SHORT_SYSTEM_PROMPT) < 500
        assert len(gen.SHORT_USER_PROMPT_TEMPLATE) < 200
        assert len(gen.SHORT_SYSTEM_PROMPT) < len(gen.SYSTEM_PROMPT) // 3


class _RecordingClient:
    """Stand-in LLMClient that records the system / user it was called with."""

    def __init__(self, **kw):
        self.kw = kw
        self.system = None
        self.user = None
        self.json_mode = None

    def chat(self, system, user, json_mode=False, **kw):
        self.system = system
        self.user = user
        self.json_mode = json_mode
        return json.dumps({
            "smiles_list": ["CCO"],
            "rationale": "test",
        })


def _make_config(provider_cfg):
    return {"llm": {"providers": provider_cfg}}


def test_short_prompt_used_when_prompt_mode_short(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(llm_mod, "get_client", lambda name, config, mock: fake)
    config = _make_config({
        "GLM": {"model": "glm-5.3-flash", "prompt_mode": "short"},
    })
    result = gen.generate_with_provider(
        provider_name="GLM", config=config, n=1, use_mock=True,
    )
    # system should be SHORT_SYSTEM_PROMPT (no references, no SAR).
    assert "ATP-binding pocket" not in fake.system
    assert "senior medicinal chemist" not in fake.system
    assert fake.json_mode is True
    assert result["smiles_list"] == ["CCO"]


def test_full_prompt_used_when_prompt_mode_absent(monkeypatch):
    fake = _RecordingClient()
    monkeypatch.setattr(llm_mod, "get_client", lambda name, config, mock: fake)
    config = _make_config({"MiniMax": {"model": "MiniMax-M3"}})
    result = gen.generate_with_provider(
        provider_name="MiniMax", config=config, n=1, use_mock=True,
    )
    # system should be SYSTEM_PROMPT (EGFR brief + references).
    assert "ATP-binding pocket" in fake.system
    assert "afatinib" in fake.system
    assert result["smiles_list"] == ["CCO"]


def test_prompt_mode_case_insensitive(monkeypatch):
    """prompt_mode: 'SHORT' and 'Short' should both trigger short mode."""
    fake = _RecordingClient()
    monkeypatch.setattr(llm_mod, "get_client", lambda name, config, mock: fake)
    config = _make_config({
        "GLM": {"model": "glm-5.3-flash", "prompt_mode": "SHORT"},
    })
    gen.generate_with_provider(
        provider_name="GLM", config=config, n=1, use_mock=True,
    )
    assert "ATP-binding pocket" not in fake.system