"""Provider factory and public re-exports."""

from __future__ import annotations

import os

from sf_dev_agent.providers.base import LLMProvider, LLMResponse, ToolCall

PROVIDERS = ("anthropic", "openai", "gemini")

# Maps each provider to the env var that holds its API key.
PROVIDER_KEY_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}


def detect_provider_from_keys() -> str | None:
    """Return the provider whose API key is set, or None if 0 or >1 are set."""
    set_providers = [
        name for name, var in PROVIDER_KEY_VARS.items()
        if os.environ.get(var, "").strip()
    ]
    return set_providers[0] if len(set_providers) == 1 else None


def create_provider(
    provider: str | None = None,
    model: str | None = None,
) -> LLMProvider:
    """Instantiate the requested LLM provider.

    Resolution order for `provider`:
      1. explicit argument
      2. LLM_PROVIDER env var
      3. auto-detect from whichever API key env var is set (only if exactly one)
      4. error with guidance

    Resolution order for `model`:
      1. explicit argument
      2. LLM_MODEL env var
      3. provider's built-in default
    """
    if not provider:
        provider = os.environ.get("LLM_PROVIDER")
    if not provider:
        provider = detect_provider_from_keys()
    if not provider:
        set_keys = [
            var for var in PROVIDER_KEY_VARS.values()
            if os.environ.get(var, "").strip()
        ]
        if not set_keys:
            raise ValueError(
                "No LLM provider configured. Set one of "
                f"{', '.join(PROVIDER_KEY_VARS.values())} in .env, "
                "or run: sf-agent setup"
            )
        raise ValueError(
            f"Multiple API keys are set ({', '.join(set_keys)}). "
            "Set LLM_PROVIDER in .env or pass --provider to disambiguate."
        )

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


__all__ = [
    "LLMProvider", "LLMResponse", "ToolCall",
    "create_provider", "detect_provider_from_keys",
    "PROVIDERS", "PROVIDER_KEY_VARS",
]
