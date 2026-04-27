"""Tests for the ESC-interrupt support (Phase C.3).

Three layers:
  - InterruptListener context manager: no-op when stdin is not a TTY,
    flag transitions on `fire_for_test()`/`reset()`, threading hygiene.
  - Agent loop integration: an interrupt mid-stream aborts cleanly,
    appends the synthetic "<user pressed ESC>" message, and exits the
    loop without firing tool calls.
  - KeyboardInterrupt path: Ctrl+C during streaming behaves the same
    way (catches at the same level as ESC).
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import pytest

from sf_dev_agent.interrupt import InterruptListener
from sf_dev_agent.providers.base import (
    LLMProvider,
    LLMResponse,
    StreamChunk,
    StreamChunkKind,
    consume_stream,
)


# ---------------------------------------------------------------------------
# InterruptListener — flag mechanics + non-TTY no-op
# ---------------------------------------------------------------------------

def test_listener_is_noop_when_stdin_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test runners pipe stdin; the listener should not start a thread."""
    monkeypatch.setattr("sys.stdin", io.StringIO())
    with InterruptListener() as listener:
        # No background thread spun up.
        assert listener._thread is None
        # Flag stays clear.
        assert not listener.is_set()


def test_fire_for_test_sets_the_flag() -> None:
    """The test hook is the canonical way to simulate an ESC press."""
    listener = InterruptListener()
    assert not listener.is_set()
    listener.fire_for_test()
    assert listener.is_set()


def test_reset_clears_the_flag() -> None:
    listener = InterruptListener()
    listener.fire_for_test()
    assert listener.is_set()
    listener.reset()
    assert not listener.is_set()


def test_listener_join_completes_on_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """__exit__ must drain the polling thread cleanly within the timeout."""
    # Force the TTY path so the thread actually starts. We can't drive a
    # real keyboard, but we can verify the thread comes down on stop.
    monkeypatch.setattr(
        "sf_dev_agent.interrupt.InterruptListener._stdin_is_tty",
        staticmethod(lambda: True),
    )
    # Replace the platform polls with a tight spin that respects _stop.
    monkeypatch.setattr(
        "sf_dev_agent.interrupt.InterruptListener._run_windows",
        lambda self: self._stop.wait(),
    )
    monkeypatch.setattr(
        "sf_dev_agent.interrupt.InterruptListener._run_posix",
        lambda self: self._stop.wait(),
    )
    listener = InterruptListener()
    with listener:
        assert listener._thread is not None
        assert listener._thread.is_alive()
    # __exit__ joins; thread should now be cleaned up.
    assert listener._thread is None


# ---------------------------------------------------------------------------
# Agent loop integration
# ---------------------------------------------------------------------------

class _ScriptedStreamingProvider(LLMProvider):
    """Yields scripted stream chunks. Tracks how many times chat_stream ran."""

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self._scripts = list(scripts)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "interrupt-test"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(iter(self._scripts.pop(0)) if self._scripts else iter([]))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        self.calls += 1
        if not self._scripts:
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")
            return
        yield from self._scripts.pop(0)


def _make_org() -> Any:
    from sf_dev_agent.models.schemas import OrgConnection
    return OrgConnection(
        tenant_id="t1", org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


def test_agent_loop_catches_interrupted_error_mid_stream(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the listener fires during text streaming, the loop aborts and
    records a synthetic interrupt message in the conversation."""
    from sf_dev_agent.agent import AgentLoop

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    # The trick: pre-fire the listener flag immediately on __enter__ so the
    # very first text delta raises. We do this by patching the listener
    # class globally for the duration of the test.
    real_enter = InterruptListener.__enter__

    def pre_fired_enter(self: InterruptListener) -> InterruptListener:
        result = real_enter(self)
        self.fire_for_test()
        return result

    monkeypatch.setattr(InterruptListener, "__enter__", pre_fired_enter)

    provider = _ScriptedStreamingProvider([
        [
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="streaming "),
            StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text="some text"),
            StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn"),
        ],
    ])

    agent = AgentLoop(
        org=_make_org(), provider=provider, mock_org=True, streaming=True,
    )
    agent.run("do a thing")

    # Synthetic user-cancel message appended after the user request.
    msgs = agent.conversation.as_messages()
    contents = [m.get("content") for m in msgs if m.get("role") == "user"]
    assert any(
        isinstance(c, str) and "<user pressed ESC" in c for c in contents
    ), f"expected interrupt sentinel in messages, got {contents!r}"

    # Only one chat_stream call — the loop returned early.
    assert provider.calls == 1


def test_agent_loop_catches_keyboard_interrupt_mid_stream(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl+C raised by consume_stream should be handled identically to ESC."""
    from sf_dev_agent.agent import AgentLoop

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    class KbProvider(LLMProvider):
        @property
        def model_name(self) -> str:
            return "kbi-test"

        def chat(self, **kwargs: Any) -> LLMResponse:
            raise KeyboardInterrupt

        def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
            raise KeyboardInterrupt
            yield  # pragma: no cover — generator marker only

    agent = AgentLoop(
        org=_make_org(), provider=KbProvider(), mock_org=True, streaming=True,
    )
    agent.run("do another thing")

    msgs = agent.conversation.as_messages()
    contents = [m.get("content") for m in msgs if m.get("role") == "user"]
    assert any(
        isinstance(c, str) and "<user pressed ESC" in c for c in contents
    )


def test_agent_loop_skips_tools_when_interrupt_fires_after_stream(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ESC fires after text but before tools dispatch (post-stream check),
    the agent must not execute any tools that the model emitted."""
    from sf_dev_agent.agent import AgentLoop
    from sf_dev_agent.tools.registry import ToolRegistry

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    executed: list[str] = []

    def tracking_execute(self: ToolRegistry, name: str, inp: dict[str, Any]) -> Any:
        executed.append(name)
        return {"ok": True}

    monkeypatch.setattr(ToolRegistry, "execute", tracking_execute)

    # Patch the listener so it returns clean from streaming (no raise) but
    # `is_set()` returns True at the post-stream poll.
    monkeypatch.setattr(
        InterruptListener, "is_set", lambda self: True,
    )
    # Reset() must be a no-op for this test or it would clear the flag at
    # the top of the loop.
    monkeypatch.setattr(
        InterruptListener, "reset", lambda self: None,
    )

    provider = _ScriptedStreamingProvider([
        [
            StreamChunk(
                kind=StreamChunkKind.TOOL_USE_START,
                tool_id="abc", tool_name="code_search",
            ),
            StreamChunk(
                kind=StreamChunkKind.TOOL_USE_END,
                tool_id="abc", tool_input={"q": "x"},
            ),
            StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use"),
        ],
    ])

    agent = AgentLoop(
        org=_make_org(), provider=provider, mock_org=True, streaming=True,
    )
    agent.run("call a tool")

    assert executed == [], (
        "expected zero tool executions when interrupted post-stream, "
        f"got {executed!r}"
    )
