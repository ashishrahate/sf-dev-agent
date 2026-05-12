"""Anthropic provider adapter."""

from __future__ import annotations

from typing import Any

try:
    import anthropic
except ImportError as exc:
    raise ImportError(
        "anthropic package not installed. "
        "Run: uv pip install 'sf-dev-agent[anthropic]'"
    ) from exc

from sf_dev_agent.providers.base import LLMProvider, LLMResponse, TokenUsage, ToolCall

DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        self._model = model
        self._client = anthropic.Anthropic(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self._model

    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 16384,
    ) -> LLMResponse:
        anthropic_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tools
        ]

        # Prompt caching (Item 3a). One ephemeral marker on the system
        # block and one on the LAST tool — together they cache the
        # entire (system + tools) prefix as a single breakpoint, which
        # is the longest stable segment across iterations of one task.
        # Anthropic charges cache writes at 1.25x normal input on the
        # first call and ~90% discount on subsequent reads while the
        # 5-minute TTL is alive. The 1024-token minimum is enforced by
        # the API; markers on shorter content are silently ignored, so
        # the markers are always safe to attach.
        system_blocks: list[dict[str, Any]] = [{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }]
        if anthropic_tools:
            anthropic_tools[-1] = {
                **anthropic_tools[-1],
                "cache_control": {"type": "ephemeral"},
            }

        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=self._clean_messages(messages),
            tools=anthropic_tools,
        )

        text_blocks: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_blocks.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        return LLMResponse(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage=_extract_usage(getattr(response, "usage", None)),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clean_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip internal-only fields that Anthropic's API does not accept."""
        cleaned = []
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                cleaned.append(msg)
                continue

            clean_content = []
            for block in content:
                if block.get("type") == "tool_result":
                    entry: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": block["tool_use_id"],
                        "content": block.get("content", ""),
                    }
                    if "is_error" in block:
                        entry["is_error"] = block["is_error"]
                    clean_content.append(entry)
                else:
                    clean_content.append(block)

            cleaned.append({**msg, "content": clean_content})
        return cleaned


def _extract_usage(raw: Any) -> TokenUsage:
    """Coerce Anthropic's response.usage object into the provider-neutral
    `TokenUsage` shape. Returns zeros when the SDK didn't report anything."""
    if raw is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=getattr(raw, "input_tokens", 0) or 0,
        output_tokens=getattr(raw, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
    )
