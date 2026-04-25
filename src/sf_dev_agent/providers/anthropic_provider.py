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

from sf_dev_agent.providers.base import LLMProvider, LLMResponse, ToolCall

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

        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
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
