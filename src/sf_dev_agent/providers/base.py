"""Abstract base for all LLM provider adapters."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class TokenUsage:
    """Per-call token accounting.

    Providers populate as many fields as their API surfaces. Defaults of 0
    mean either "the model doesn't bill for this category" or "the SDK
    didn't return it." Audit aggregations can sum across fields without
    None-checks.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    # Cache hits — input tokens that came back at a discount because the
    # provider matched them against a cached prefix. Anthropic reports this
    # as `cache_read_input_tokens`; OpenAI's `prompt_tokens_details.cached_tokens`;
    # Gemini's `cached_content_token_count`. All three map here.
    cache_read_tokens: int = 0
    # Cache writes — input tokens billed at the cache-creation rate the
    # first time a marker fires. Anthropic surfaces this; the others don't
    # (their caching is implicit / free at write time).
    cache_write_tokens: int = 0


@dataclass
class LLMResponse:
    text_blocks: list[str] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: TokenUsage = field(default_factory=TokenUsage)


class StreamChunkKind(StrEnum):
    """Categorical tag for the StreamChunk discriminated union.

    Order of events for a typical streamed message:

        TEXT_DELTA*           (zero or more, in pieces)
        TOOL_USE_START
            TOOL_USE_DELTA*   (zero or more JSON fragments)
        TOOL_USE_END
        ...                   (more text or more tool uses)
        STOP                  (exactly one, terminal)
    """

    TEXT_DELTA = "text_delta"
    TOOL_USE_START = "tool_use_start"
    TOOL_USE_DELTA = "tool_use_delta"
    TOOL_USE_END = "tool_use_end"
    STOP = "stop"


@dataclass
class StreamChunk:
    """One event from `LLMProvider.chat_stream`.

    Provider-agnostic shape that maps cleanly onto Anthropic's typed
    block-delta events, OpenAI's tool_call deltas, and Gemini's
    streaming response parts.
    """
    kind: StreamChunkKind
    # text_delta: token(s) to append.
    text: str = ""
    # tool_use_*: identifier of the tool block this chunk applies to.
    tool_id: str = ""
    # tool_use_start: the tool's name (set once per block).
    tool_name: str = ""
    # tool_use_delta: a JSON fragment of the tool's input. Concatenate
    # all deltas for one tool_id, then json.loads at TOOL_USE_END.
    tool_input_json: str = ""
    # tool_use_end: parsed input dict (providers that buffer the JSON
    # internally and emit the parsed form at end). May be {} if the
    # provider only emits deltas; consumer reconstructs from those.
    tool_input: dict[str, Any] = field(default_factory=dict)
    # stop: terminal reason.
    stop_reason: str = ""
    # stop: token usage for the call as a whole. Providers that stream
    # natively (Gemini) accumulate from each chunk's usage_metadata and
    # attach to the STOP chunk. The default chat_stream fallback (used by
    # providers without true streaming) copies LLMResponse.usage into here.
    usage: TokenUsage | None = None


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

    # ------------------------------------------------------------------
    # Streaming — Phase C.2.
    #
    # Default implementation calls `self.chat()` and yields one big
    # batch of StreamChunks at the end. Real streaming providers
    # override this method to yield chunks as the LLM produces them
    # (e.g. text deltas mid-message), which the REPL renders live.
    # ------------------------------------------------------------------

    def chat_stream(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 16384,
    ) -> Iterator[StreamChunk]:
        """Default fallback — call chat() and emit synthetic chunks.

        Providers that want true mid-message streaming should override
        this method. Until they do, callers using `chat_stream` get the
        same results as `chat()` but with a uniform iterator surface,
        so the agent loop only has one code path.
        """
        response = self.chat(
            system=system, messages=messages,
            tools=tools, max_tokens=max_tokens,
        )
        for block in response.text_blocks:
            yield StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text=block)
        for call in response.tool_calls:
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_START,
                tool_id=call.id, tool_name=call.name,
            )
            # Synthesize a single delta carrying the full input JSON; then
            # an END carrying the parsed dict. Either one is sufficient
            # for the consumer to reconstruct.
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_DELTA,
                tool_id=call.id,
                tool_input_json=json.dumps(call.input),
            )
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_END,
                tool_id=call.id, tool_input=dict(call.input),
            )
        yield StreamChunk(
            kind=StreamChunkKind.STOP,
            stop_reason=response.stop_reason,
            usage=response.usage,
        )


# ---------------------------------------------------------------------------
# Stream consumer helper — turn a chunk iterator back into an LLMResponse
# ---------------------------------------------------------------------------

def consume_stream(
    chunks: Iterator[StreamChunk],
    on_text: Any = None,
) -> LLMResponse:
    """Drain a chat_stream iterator into an LLMResponse.

    `on_text` is an optional callback invoked with each text delta — the
    REPL hooks live rendering through here. Providers that want unified
    behavior between `chat()` and `chat_stream()` can override `chat()`
    to call this helper.
    """
    text_blocks: list[str] = []
    current_text: list[str] = []
    tool_calls: list[ToolCall] = []
    open_tools: dict[str, dict[str, Any]] = {}
    stop_reason = "end_turn"
    usage: TokenUsage = TokenUsage()

    def _flush_text() -> None:
        nonlocal current_text
        if current_text:
            text_blocks.append("".join(current_text))
            current_text = []

    for chunk in chunks:
        if chunk.kind == StreamChunkKind.TEXT_DELTA:
            current_text.append(chunk.text)
            if on_text is not None and chunk.text:
                on_text(chunk.text)
        elif chunk.kind == StreamChunkKind.TOOL_USE_START:
            _flush_text()
            open_tools[chunk.tool_id] = {
                "name": chunk.tool_name, "input_buf": "", "input": {},
            }
        elif chunk.kind == StreamChunkKind.TOOL_USE_DELTA:
            entry = open_tools.get(chunk.tool_id)
            if entry is None:
                continue
            entry["input_buf"] += chunk.tool_input_json
        elif chunk.kind == StreamChunkKind.TOOL_USE_END:
            entry = open_tools.pop(chunk.tool_id, None)
            if entry is None:
                continue
            # Prefer the parsed dict if the provider supplied one;
            # otherwise parse the buffered JSON (synthesised by the default
            # chat_stream() implementation).
            input_data = chunk.tool_input or _safe_json_loads(entry["input_buf"])
            tool_calls.append(ToolCall(
                id=chunk.tool_id, name=entry["name"], input=input_data,
            ))
        elif chunk.kind == StreamChunkKind.STOP:
            _flush_text()
            stop_reason = chunk.stop_reason or stop_reason
            if chunk.usage is not None:
                usage = chunk.usage
            break

    _flush_text()
    return LLMResponse(
        text_blocks=text_blocks, tool_calls=tool_calls,
        stop_reason=stop_reason, usage=usage,
    )


def _safe_json_loads(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}
