"""Unit tests for the streaming abstraction (Phase C.2).

Three layers:
  - StreamChunk + consume_stream helper (provider-agnostic).
  - LLMProvider's default chat_stream() that wraps chat() — verifies
    every existing provider gets free pseudo-streaming.
  - Agent loop's `streaming=True` path renders deltas as they arrive.

GeminiProvider's real chat_stream is exercised via mocked SDK chunks —
no live API calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sf_dev_agent.providers.base import (
    LLMProvider,
    LLMResponse,
    StreamChunk,
    StreamChunkKind,
    ToolCall,
    consume_stream,
)

# ---------------------------------------------------------------------------
# consume_stream — turn StreamChunks back into an LLMResponse
# ---------------------------------------------------------------------------

def test_consume_stream_text_only() -> None:
    chunks = [
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="Hello, "),
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="world."),
        StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn"),
    ]
    resp = consume_stream(iter(chunks))
    assert resp.text_blocks == ["Hello, world."]
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"


def test_consume_stream_invokes_on_text_callback() -> None:
    captured: list[str] = []
    chunks = [
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="a"),
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="b"),
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="c"),
        StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn"),
    ]
    consume_stream(iter(chunks), on_text=captured.append)
    assert captured == ["a", "b", "c"]


def test_consume_stream_tool_use_with_input_dict() -> None:
    """Provider supplies parsed input on TOOL_USE_END."""
    chunks = [
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="Calling tool: "),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_START,
            tool_id="t1", tool_name="describe",
        ),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_END,
            tool_id="t1", tool_input={"object": "Account"},
        ),
        StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use"),
    ]
    resp = consume_stream(iter(chunks))
    assert resp.text_blocks == ["Calling tool: "]
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "t1"
    assert resp.tool_calls[0].name == "describe"
    assert resp.tool_calls[0].input == {"object": "Account"}
    assert resp.stop_reason == "tool_use"


def test_consume_stream_tool_use_with_json_deltas() -> None:
    """Provider streams the tool input as JSON fragments."""
    chunks = [
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_START,
            tool_id="t1", tool_name="echo",
        ),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_DELTA,
            tool_id="t1", tool_input_json='{"msg":',
        ),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_DELTA,
            tool_id="t1", tool_input_json=' "hi"}',
        ),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_END,
            tool_id="t1",  # no parsed dict — consumer reconstructs
        ),
        StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use"),
    ]
    resp = consume_stream(iter(chunks))
    assert resp.tool_calls[0].input == {"msg": "hi"}


def test_consume_stream_text_split_by_tool_use() -> None:
    """Text chunks before and after a tool call become separate text_blocks."""
    chunks = [
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="before"),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_START,
            tool_id="t1", tool_name="x",
        ),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_END,
            tool_id="t1", tool_input={"a": 1},
        ),
        StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="after"),
        StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use"),
    ]
    resp = consume_stream(iter(chunks))
    assert resp.text_blocks == ["before", "after"]
    assert len(resp.tool_calls) == 1


def test_consume_stream_handles_malformed_json_in_deltas() -> None:
    chunks = [
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_START,
            tool_id="t1", tool_name="x",
        ),
        StreamChunk(
            kind=StreamChunkKind.TOOL_USE_DELTA,
            tool_id="t1", tool_input_json="not valid {{",
        ),
        StreamChunk(kind=StreamChunkKind.TOOL_USE_END, tool_id="t1"),
        StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use"),
    ]
    resp = consume_stream(iter(chunks))
    # Malformed JSON falls back to {} — agent gets an empty input dict
    # rather than a parse exception killing the loop.
    assert resp.tool_calls[0].input == {}


def test_consume_stream_unknown_tool_end_id_ignored() -> None:
    """An END for a tool we never saw a START for is dropped silently."""
    chunks = [
        StreamChunk(kind=StreamChunkKind.TOOL_USE_END, tool_id="ghost"),
        StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn"),
    ]
    resp = consume_stream(iter(chunks))
    assert resp.tool_calls == []


# ---------------------------------------------------------------------------
# LLMProvider default chat_stream wraps chat()
# ---------------------------------------------------------------------------

class _UnaryProvider(LLMProvider):
    """Test provider that only implements chat() — uses the default chat_stream."""

    def __init__(self, response: LLMResponse) -> None:
        self._response = response

    @property
    def model_name(self) -> str:
        return "unary-test"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return self._response


def test_default_chat_stream_yields_text_chunks() -> None:
    provider = _UnaryProvider(LLMResponse(
        text_blocks=["one", "two"],
        tool_calls=[],
        stop_reason="end_turn",
    ))
    chunks = list(provider.chat_stream(system="", messages=[], tools=[]))
    text_chunks = [c for c in chunks if c.kind == StreamChunkKind.TEXT_DELTA]
    assert [c.text for c in text_chunks] == ["one", "two"]
    assert chunks[-1].kind == StreamChunkKind.STOP


def test_default_chat_stream_round_trips_through_consume_stream() -> None:
    """chat() → chat_stream → consume_stream produces an equivalent response."""
    original = LLMResponse(
        text_blocks=["intro ", "more"],
        tool_calls=[ToolCall(id="t1", name="describe", input={"x": 1})],
        stop_reason="tool_use",
    )
    provider = _UnaryProvider(original)
    rebuilt = consume_stream(provider.chat_stream(system="", messages=[], tools=[]))
    # Text blocks may merge into one (consume_stream merges adjacent
    # text_deltas not split by a tool block).
    assert "".join(rebuilt.text_blocks) == "".join(original.text_blocks)
    assert len(rebuilt.tool_calls) == 1
    assert rebuilt.tool_calls[0].id == "t1"
    assert rebuilt.tool_calls[0].input == {"x": 1}
    assert rebuilt.stop_reason == "tool_use"


# ---------------------------------------------------------------------------
# Agent loop respects the streaming flag
# ---------------------------------------------------------------------------

class _StreamingProvider(LLMProvider):
    """Yields scripted StreamChunks. Used to verify agent's streaming path."""

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self._scripts = list(scripts)

    @property
    def model_name(self) -> str:
        return "streaming-test"

    def chat(self, **kwargs: Any) -> LLMResponse:
        # The agent loop should NOT call chat() when streaming, but provide
        # a fallback in case a code path slips through.
        chunks = self._scripts.pop(0) if self._scripts else []
        return consume_stream(iter(chunks))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        if not self._scripts:
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")
            return
        yield from self._scripts.pop(0)


def test_agent_streaming_renders_text_deltas_live(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With streaming=True, the agent prints each delta as it arrives."""
    from sf_dev_agent.agent import AgentLoop
    from sf_dev_agent.models.schemas import OrgConnection

    org = OrgConnection(
        tenant_id="t1", org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )
    provider = _StreamingProvider([
        [
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="Hello "),
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="world"),
            StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn"),
        ],
    ])

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    agent = AgentLoop(
        org=org, provider=provider, mock_org=True, streaming=True,
    )
    agent.run("hello")

    out = capsys.readouterr().out
    # The streaming path uses console.print(end="") — each delta lands
    # in the captured stdout as raw text.
    assert "Hello world" in out


def test_agent_non_streaming_buffers_full_text(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With streaming=False, deltas are buffered and the full text renders
    via Markdown at end-of-message."""
    from sf_dev_agent.agent import AgentLoop
    from sf_dev_agent.models.schemas import OrgConnection

    org = OrgConnection(
        tenant_id="t1", org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )
    provider = _StreamingProvider([
        [
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="bufferable "),
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="response"),
            StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn"),
        ],
    ])

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    agent = AgentLoop(
        org=org, provider=provider, mock_org=True, streaming=False,
    )
    agent.run("hi")

    # Both modes ultimately produce the full text on stdout.
    out = capsys.readouterr().out
    assert "bufferable response" in out


def test_agent_streaming_handles_tool_calls(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tool calls in the stream get executed; conversation grows correctly."""
    from sf_dev_agent.agent import AgentLoop
    from sf_dev_agent.models.schemas import OrgConnection

    org = OrgConnection(
        tenant_id="t1", org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )

    # First call: text + a tool_use. Second call: just text + end_turn.
    provider = _StreamingProvider([
        [
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="thinking..."),
            StreamChunk(
                kind=StreamChunkKind.TOOL_USE_START,
                tool_id="t1", tool_name="sf_metadata_describe",
            ),
            StreamChunk(
                kind=StreamChunkKind.TOOL_USE_END,
                tool_id="t1", tool_input={"component_type": "ApexClass"},
            ),
            StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use"),
        ],
        [
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="done"),
            StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn"),
        ],
    ])

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    agent = AgentLoop(
        org=org, provider=provider, mock_org=True, streaming=True,
    )
    agent.run("describe Apex classes")

    # Conversation: original user request + assistant turn 1 (text +
    # tool_use) + tool_results + assistant turn 2 (text only).
    msgs = list(agent.conversation)
    assert len(msgs) >= 3
    assistant_turns = [m for m in msgs if m["role"] == "assistant"]
    assert any(
        any(b.get("type") == "tool_use" for b in m["content"])
        for m in assistant_turns
    )


# ---------------------------------------------------------------------------
# GeminiProvider real chat_stream — mocked SDK
# ---------------------------------------------------------------------------

def _make_part(text: str = "", function_call: Any = None) -> Any:
    """Minimal duck-type for a Gemini stream part."""
    class P:
        pass
    p = P()
    p.text = text
    p.function_call = function_call
    return p


def _make_chunk(parts: list[Any]) -> Any:
    """Minimal duck-type for a Gemini stream chunk."""
    class Candidate:
        pass
    class Content:
        pass
    class Chunk:
        pass

    cand = Candidate()
    cand.content = Content()
    cand.content.parts = parts
    chunk = Chunk()
    chunk.candidates = [cand]
    return chunk


def test_gemini_chat_stream_yields_text_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini's chat_stream emits TEXT_DELTA chunks as the SDK streams them."""
    pytest.importorskip("google.genai")
    from sf_dev_agent.providers.gemini_provider import GeminiProvider

    captured_calls: dict[str, Any] = {}

    def fake_stream_method(**kwargs: Any) -> Iterator[Any]:
        captured_calls["kwargs"] = kwargs
        yield _make_chunk([_make_part(text="Hel")])
        yield _make_chunk([_make_part(text="lo")])
        yield _make_chunk([_make_part(text=" world")])

    class _FakeModels:
        def generate_content_stream(self, **kwargs: Any) -> Iterator[Any]:
            return fake_stream_method(**kwargs)

    class _FakeClient:
        models = _FakeModels()

    provider = GeminiProvider(model="gemini-test", api_key="fake")
    monkeypatch.setattr(provider, "_get_client", lambda: _FakeClient())

    chunks = list(provider.chat_stream(
        system="sys", messages=[{"role": "user", "content": "hi"}], tools=[],
    ))

    text_chunks = [c for c in chunks if c.kind == StreamChunkKind.TEXT_DELTA]
    assert [c.text for c in text_chunks] == ["Hel", "lo", " world"]
    assert chunks[-1].kind == StreamChunkKind.STOP
    assert chunks[-1].stop_reason == "end_turn"


def test_gemini_chat_stream_emits_tool_use_at_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini doesn't delta-stream function_calls; provider buffers and
    emits TOOL_USE_START/END pairs at end-of-stream with stop=tool_use."""
    pytest.importorskip("google.genai")
    from sf_dev_agent.providers.gemini_provider import GeminiProvider

    class FakeFC:
        name = "describe"
        args = {"object": "Account"}

    def fake_stream() -> Iterator[Any]:
        yield _make_chunk([_make_part(text="Calling tool")])
        yield _make_chunk([_make_part(function_call=FakeFC())])

    class _FakeModels:
        def generate_content_stream(self, **kwargs: Any) -> Iterator[Any]:
            return fake_stream()

    class _FakeClient:
        models = _FakeModels()

    provider = GeminiProvider(model="gemini-test", api_key="fake")
    monkeypatch.setattr(provider, "_get_client", lambda: _FakeClient())

    chunks = list(provider.chat_stream(
        system="sys", messages=[{"role": "user", "content": "x"}], tools=[],
    ))

    starts = [c for c in chunks if c.kind == StreamChunkKind.TOOL_USE_START]
    ends = [c for c in chunks if c.kind == StreamChunkKind.TOOL_USE_END]
    assert len(starts) == 1
    assert starts[0].tool_name == "describe"
    assert ends[0].tool_input == {"object": "Account"}
    assert chunks[-1].kind == StreamChunkKind.STOP
    assert chunks[-1].stop_reason == "tool_use"


def test_gemini_chat_stream_quota_zero_raises_friendly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'limit: 0' 429 from a paid-only model should raise a helpful error."""
    pytest.importorskip("google.genai")
    from google.genai import errors as genai_errors

    from sf_dev_agent.providers.gemini_provider import GeminiProvider

    class _FakeModels:
        def generate_content_stream(self, **kwargs: Any) -> Iterator[Any]:
            raise genai_errors.ClientError(
                code=429,
                response_json={"error": {"message": "limit: 0 quota exhausted"}},
            )

    class _FakeClient:
        models = _FakeModels()

    provider = GeminiProvider(model="gemini-test", api_key="fake")
    monkeypatch.setattr(provider, "_get_client", lambda: _FakeClient())

    with pytest.raises(RuntimeError, match="no free-tier quota"):
        list(provider.chat_stream(
            system="", messages=[{"role": "user", "content": "x"}], tools=[],
        ))
