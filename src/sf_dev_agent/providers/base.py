"""Abstract base for all LLM provider adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text_blocks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"


class LLMProvider(ABC):
    """Adapter interface every provider must implement.

    Internal message format follows Anthropic's structure (it is the most
    explicit for tool-use). Each concrete provider converts from this format
    to its native API format before sending and converts the response back
    into an LLMResponse before returning.
    """

    @abstractmethod
    def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 16384,
    ) -> LLMResponse: ...

    @property
    @abstractmethod
    def model_name(self) -> str: ...
