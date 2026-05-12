"""Tests for prompt-caching wiring + token-usage extraction across providers.

Item 3a — verifies the Anthropic provider attaches `cache_control` markers
to the system block and the last tool definition (caches the entire stable
prefix across iterations of one task).

Item 2 follow-on — verifies each provider's `_extract_usage` helper maps
the raw SDK shape onto the provider-neutral `TokenUsage` dataclass.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Anthropic — cache_control markers on system + tools
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_anthropic_response():
    """Stand-in for `anthropic.Anthropic().messages.create(...)` return."""
    resp = MagicMock()
    resp.content = []  # empty content -> no text blocks / tool calls
    resp.stop_reason = "end_turn"
    usage = MagicMock()
    usage.input_tokens = 1500
    usage.output_tokens = 20
    usage.cache_read_input_tokens = 1200
    usage.cache_creation_input_tokens = 300
    resp.usage = usage
    return resp


def test_anthropic_chat_attaches_cache_markers(
    monkeypatch: pytest.MonkeyPatch, fake_anthropic_response,
) -> None:
    """The Anthropic client must be called with system-as-block + cache_control
    on system and on the last tool. Captured kwargs let us assert without
    touching the real SDK."""
    pytest.importorskip("anthropic")
    from sf_dev_agent.providers.anthropic_provider import AnthropicProvider

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_anthropic_response
    monkeypatch.setattr(
        "sf_dev_agent.providers.anthropic_provider.anthropic.Anthropic",
        lambda **_: fake_client,
    )

    provider = AnthropicProvider(api_key="not-used")
    response = provider.chat(
        system="you are a helpful agent",
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {"name": "t1", "description": "first", "parameters": {"type": "object"}},
            {"name": "t2", "description": "second", "parameters": {"type": "object"}},
        ],
    )

    call_kwargs = fake_client.messages.create.call_args.kwargs
    # System came through as a list of blocks with cache_control on the
    # single text block.
    system_blocks = call_kwargs["system"]
    assert isinstance(system_blocks, list)
    assert system_blocks[0]["type"] == "text"
    assert system_blocks[0]["text"] == "you are a helpful agent"
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}
    # Tools — only the LAST tool gets a cache marker (one breakpoint covers
    # the entire prefix). Earlier tools must not carry the marker.
    tools = call_kwargs["tools"]
    assert len(tools) == 2
    assert "cache_control" not in tools[0]
    assert tools[1]["cache_control"] == {"type": "ephemeral"}
    # Sanity: usage propagation downstream still works.
    assert response.usage.input_tokens == 1500
    assert response.usage.cache_read_tokens == 1200
    assert response.usage.cache_write_tokens == 300


def test_anthropic_chat_handles_zero_tools(
    monkeypatch: pytest.MonkeyPatch, fake_anthropic_response,
) -> None:
    """When the agent passes an empty tool list (e.g., a no-tool turn), the
    cache marker on tools is skipped — system still carries one."""
    pytest.importorskip("anthropic")
    from sf_dev_agent.providers.anthropic_provider import AnthropicProvider

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_anthropic_response
    monkeypatch.setattr(
        "sf_dev_agent.providers.anthropic_provider.anthropic.Anthropic",
        lambda **_: fake_client,
    )

    provider = AnthropicProvider(api_key="not-used")
    provider.chat(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
    )
    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert call_kwargs["tools"] == []
    # System marker still present.
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Usage extraction — provider-neutral mapping
# ---------------------------------------------------------------------------

def test_anthropic_extract_usage_maps_all_fields() -> None:
    pytest.importorskip("anthropic")
    from sf_dev_agent.providers.anthropic_provider import _extract_usage

    raw = MagicMock(
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=80,
        cache_creation_input_tokens=10,
    )
    usage = _extract_usage(raw)
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20
    assert usage.cache_read_tokens == 80
    assert usage.cache_write_tokens == 10


def test_anthropic_extract_usage_handles_none() -> None:
    pytest.importorskip("anthropic")
    from sf_dev_agent.providers.anthropic_provider import _extract_usage

    usage = _extract_usage(None)
    assert (
        usage.input_tokens == 0
        and usage.output_tokens == 0
        and usage.cache_read_tokens == 0
        and usage.cache_write_tokens == 0
    )


def test_openai_extract_usage_pulls_cached_tokens_from_details() -> None:
    pytest.importorskip("openai")
    from sf_dev_agent.providers.openai_provider import _extract_usage

    details = MagicMock(cached_tokens=64)
    raw = MagicMock(
        prompt_tokens=200,
        completion_tokens=30,
        prompt_tokens_details=details,
    )
    usage = _extract_usage(raw)
    assert usage.input_tokens == 200
    assert usage.output_tokens == 30
    assert usage.cache_read_tokens == 64
    # OpenAI's cache is write-free.
    assert usage.cache_write_tokens == 0


def test_openai_extract_usage_missing_details_field_is_zero() -> None:
    pytest.importorskip("openai")
    from sf_dev_agent.providers.openai_provider import _extract_usage

    raw = MagicMock(spec=["prompt_tokens", "completion_tokens"])
    raw.prompt_tokens = 50
    raw.completion_tokens = 5
    usage = _extract_usage(raw)
    assert usage.input_tokens == 50
    assert usage.output_tokens == 5
    assert usage.cache_read_tokens == 0


def test_gemini_extract_usage_maps_all_fields() -> None:
    pytest.importorskip("google.genai")
    from sf_dev_agent.providers.gemini_provider import _extract_usage

    raw = MagicMock(
        prompt_token_count=300,
        candidates_token_count=40,
        cached_content_token_count=120,
    )
    usage = _extract_usage(raw)
    assert usage.input_tokens == 300
    assert usage.output_tokens == 40
    assert usage.cache_read_tokens == 120
    assert usage.cache_write_tokens == 0


def test_gemini_extract_usage_handles_none() -> None:
    pytest.importorskip("google.genai")
    from sf_dev_agent.providers.gemini_provider import _extract_usage

    usage = _extract_usage(None)
    assert (
        usage.input_tokens == 0
        and usage.output_tokens == 0
        and usage.cache_read_tokens == 0
    )
