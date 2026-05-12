"""OpenAI provider adapter."""

from __future__ import annotations

import json
from typing import Any

try:
    from openai import NOT_GIVEN, OpenAI
except ImportError as exc:
    raise ImportError(
        "openai package not installed. "
        "Run: uv pip install 'sf-dev-agent[openai]'"
    ) from exc

from sf_dev_agent.providers.base import LLMProvider, LLMResponse, TokenUsage, ToolCall

DEFAULT_MODEL = "gpt-4o"


class OpenAIProvider(LLMProvider):
    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        self._model = model
        self._client = OpenAI(api_key=api_key)

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
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tools
        ]

        all_messages = [{"role": "system", "content": system}] + self._convert_messages(messages)

        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=all_messages,
            tools=openai_tools if openai_tools else NOT_GIVEN,
        )

        choice = response.choices[0]
        message = choice.message

        text_blocks = [message.content] if message.content else []
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    input_data = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    input_data = {}
                tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, input=input_data))

        return LLMResponse(
            text_blocks=text_blocks,
            tool_calls=tool_calls,
            stop_reason="tool_use" if tool_calls else "end_turn",
            usage=_extract_usage(getattr(response, "usage", None)),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _convert_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert internal Anthropic-like messages to OpenAI chat format."""
        result: list[dict[str, Any]] = []

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if isinstance(content, str):
                result.append({"role": role, "content": content})
                continue

            text_parts: list[str] = []
            tool_calls_out: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []

            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block["text"])
                elif btype == "tool_use":
                    tool_calls_out.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"]),
                        },
                    })
                elif btype == "tool_result":
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": str(block.get("content", "")),
                    })

            if tool_results:
                result.extend(tool_results)
            elif tool_calls_out:
                result.append({
                    "role": "assistant",
                    "content": " ".join(text_parts) or None,
                    "tool_calls": tool_calls_out,
                })
            else:
                result.append({"role": role, "content": " ".join(text_parts)})

        return result


def _extract_usage(raw: Any) -> TokenUsage:
    """Map OpenAI's `usage` object onto the provider-neutral `TokenUsage`.

    The `prompt_tokens_details.cached_tokens` field surfaces cache hits on
    GPT-4o family; older models don't populate it. Defaults of 0 keep
    aggregation arithmetic clean either way.
    """
    if raw is None:
        return TokenUsage()
    cache_read = 0
    details = getattr(raw, "prompt_tokens_details", None)
    if details is not None:
        cache_read = getattr(details, "cached_tokens", 0) or 0
    return TokenUsage(
        input_tokens=getattr(raw, "prompt_tokens", 0) or 0,
        output_tokens=getattr(raw, "completion_tokens", 0) or 0,
        cache_read_tokens=cache_read,
        cache_write_tokens=0,  # OpenAI's cache is write-free.
    )
