"""Provider factory and public re-exports."""

from __future__ import annotations

import os

from sf_dev_agent.providers.base import LLMProvider, LLMResponse, ToolCall

PROVIDERS = ("anthropic", "openai", "gemini")


def create_provider(
    provider: str | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Instantiate the requested LLM provider.

    Resolution order for `provider`:
      1. explicit argument
      2. LLM_PROVIDER env var
      3. "anthropic" (default)

    Resolution order for `model`:
      1. explicit argument
      2. LLM_MODEL env var
      3. provider's built-in default
    """
    provider = provider or os.environ.get("LLM_PROVIDER", "anthropic")
    model = model or os.environ.get("LLM_MODEL") or None

    if provider == "anthropic":
        from sf_dev_agent.providers.anthropic_provider import AnthropicProvider, DEFAULT_MODEL
        return AnthropicProvider(
            model=model or DEFAULT_MODEL,
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
        )

    if provider == "openai":
        from sf_dev_agent.providers.openai_provider import OpenAIProvider, DEFAULT_MODEL
        return OpenAIProvider(
            model=model or DEFAULT_MODEL,
            api_key=os.environ.get("OPENAI_API_KEY"),
        )

    if provider == "gemini":
        from sf_dev_agent.providers.gemini_provider import GeminiProvider, DEFAULT_MODEL
        return GeminiProvider(
            model=model or DEFAULT_MODEL,
            api_key=os.environ.get("GOOGLE_API_KEY"),
        )

    raise ValueError(
        f"Unknown provider '{provider}'. "
        f"Valid choices: {', '.join(PROVIDERS)}"
    )


__all__ = ["LLMProvider", "LLMResponse", "ToolCall", "create_provider", "PROVIDERS"]
