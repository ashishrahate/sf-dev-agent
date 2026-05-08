"""Tests for agent operating modes — Slice A (core mode infrastructure).

Three modes:
  - AgentMode.PLAN       — current behavior (plan + bulk approval)
  - AgentMode.EXECUTION  — autonomous, writes pass through
  - AgentMode.GENERAL    — read-only-default, per-write inline approval

These tests exercise the gating in `AgentLoop._execute_tool`, the run-flow
dispatch in `AgentLoop.run`, the system-prompt mode override, the
`submit_plan` filtering from tool defs, the inline-approval prompt, and
the per-session allowlist.

Slice B (REPL keybindings, /mode toolbar, autosuggest) and Slice C
(persistence + resume preserves mode) are NOT covered here — those
land in their own test files.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.agent import (
    AgentLoop,
    _MODE_INSTRUCTIONS,
    _mode_instructions,
)
from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.models.schemas import AgentMode, OrgConnection
from sf_dev_agent.providers.base import (
    LLMProvider,
    LLMResponse,
    StreamChunk,
    StreamChunkKind,
    consume_stream,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1", org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


@pytest.fixture
def wm(tmp_path: Path) -> Iterator[WorkingMemoryStore]:
    store = WorkingMemoryStore(tmp_path / "wm.db")
    yield store
    store.close()


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AGENT_WORKSPACE at a tmp dir + redirect default_db_path so
    tests don't touch the user's real ~/.sf-agent state."""
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )
    return tmp_path


class _ScriptedProvider(LLMProvider):
    """Streaming provider that emits a scripted sequence of tool calls.

    Each call to chat_stream pops the next item from `script`. Items are
    either:
        ("text", str)                — emit a text delta then end_turn
        ("tool", name, input_dict)   — emit one tool_use chunk
    Once script is exhausted, subsequent calls just emit end_turn.
    """

    def __init__(self, script: list[tuple]) -> None:
        self.script = list(script)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "scripted"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(self.chat_stream(**kwargs))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        # Capture which tools the agent showed us (slice-A regression check).
        self.last_tools = kwargs.get("tools", [])
        self.calls += 1

        if not self.script:
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")
            return

        item = self.script.pop(0)
        if item[0] == "text":
            yield StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text=item[1])
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")
        elif item[0] == "tool":
            _, name, tool_input = item
            tool_id = f"tu_{self.calls}"
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_START,
                tool_id=tool_id, tool_name=name,
            )
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_END,
                tool_id=tool_id, tool_input=tool_input,
            )
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use")


# ---------------------------------------------------------------------------
# Mode metadata + system prompt rendering
# ---------------------------------------------------------------------------

def test_mode_instructions_plan_is_empty() -> None:
    """Plan mode keeps the existing prompt body authoritative — empty override."""
    assert _mode_instructions(AgentMode.PLAN) == ""


def test_mode_instructions_execution_says_skip_submit_plan() -> None:
    block = _mode_instructions(AgentMode.EXECUTION)
    assert "EXECUTION MODE" in block
    assert "submit_plan" in block
    assert "DO NOT" in block.upper()


def test_mode_instructions_general_says_per_write_approval() -> None:
    block = _mode_instructions(AgentMode.GENERAL)
    assert "GENERAL MODE" in block
    assert "submit_plan" in block
    assert "approve" in block.lower()


def test_mode_instructions_unknown_mode_falls_back_safely() -> None:
    """Defensive: a mode value not in the map returns "" rather than
    KeyError. Matters if someone adds a new enum value mid-development."""
    # Pass a bogus enum-like object — _mode_instructions only does dict.get
    class _Fake:
        value = "future"
    assert _mode_instructions(_Fake()) == ""  # type: ignore[arg-type]


def test_system_prompt_renders_mode_block_for_each_mode(
    org: OrgConnection,
) -> None:
    """The {{AGENT_MODE_INSTRUCTIONS}} placeholder gets the right block."""
    for mode in AgentMode:
        provider = _ScriptedProvider([])
        agent = AgentLoop(
            org=org, provider=provider, mock_org=True, mode=mode,
        )
        # Plan mode: empty block, but the placeholder should be replaced
        # (i.e., the literal "{{AGENT_MODE_INSTRUCTIONS}}" is gone).
        assert "{{AGENT_MODE_INSTRUCTIONS}}" not in agent.system_prompt
        # Mode-specific body shows up for non-plan.
        if mode == AgentMode.EXECUTION:
            assert "EXECUTION MODE" in agent.system_prompt
        elif mode == AgentMode.GENERAL:
            assert "GENERAL MODE" in agent.system_prompt


# ---------------------------------------------------------------------------
# AgentLoop construction defaults
# ---------------------------------------------------------------------------

def test_agentloop_defaults_to_plan_mode(org: OrgConnection) -> None:
    """Backwards compat: existing call sites that don't pass mode get PLAN."""
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
    )
    assert agent.mode == AgentMode.PLAN
    assert agent._write_allowlist == set()


def test_agentloop_accepts_shared_write_allowlist(org: OrgConnection) -> None:
    """Caller-supplied allowlist set persists modifications across the
    AgentLoop's lifetime — this is how the REPL gets per-session scope."""
    shared: set[str] = set()
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        mode=AgentMode.GENERAL, write_allowlist=shared,
    )
    agent._write_allowlist.add("file_write")
    assert shared == {"file_write"}  # caller sees the mutation


# ---------------------------------------------------------------------------
# Tool definition filtering
# ---------------------------------------------------------------------------

def test_submit_plan_present_in_plan_mode(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN,
    )
    names = {d["name"] for d in agent._mode_filtered_tool_definitions()}
    assert "submit_plan" in names


def test_submit_plan_hidden_in_execution_mode(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION,
    )
    names = {d["name"] for d in agent._mode_filtered_tool_definitions()}
    assert "submit_plan" not in names
    # Read-only tools should still be present — we only filter submit_plan.
    assert "code_search" in names


def test_submit_plan_hidden_in_general_mode(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL,
    )
    names = {d["name"] for d in agent._mode_filtered_tool_definitions()}
    assert "submit_plan" not in names


# ---------------------------------------------------------------------------
# submit_plan defensive intercept in non-plan modes
# ---------------------------------------------------------------------------

def test_submit_plan_in_execution_mode_returns_clarifying_error(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """If stale context causes the LLM to call submit_plan in execution
    mode, the intercept returns a clarifying tool_result instead of
    actually registering a plan."""
    provider = _ScriptedProvider([
        ("tool", "submit_plan", {"summary": "x", "steps": []}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    task = agent.run("just chatting")
    assert task.plan is None
    msgs = agent.conversation.as_messages()
    found = False
    for m in msgs:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if "execution" in b.get("content", "").lower():
                        found = True
    assert found, "expected a clarifying tool_result mentioning execution mode"


def test_submit_plan_in_general_mode_returns_clarifying_error(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """Same defensive intercept covers general mode."""
    provider = _ScriptedProvider([
        ("tool", "submit_plan", {"summary": "y", "steps": []}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
    )
    task = agent.run("read-only chat")
    assert task.plan is None
    msgs = agent.conversation.as_messages()
    found = False
    for m in msgs:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if "general" in b.get("content", "").lower():
                        found = True
    assert found, "expected a clarifying tool_result mentioning general mode"


# ---------------------------------------------------------------------------
# Run-flow dispatch
# ---------------------------------------------------------------------------

def test_plan_mode_runs_planning_phase(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """Plan mode goes through Phase 1 (planning) — agent transitions to
    PLANNING then awaits a submit_plan call. Without one, _run_approval
    short-circuits to COMPLETE on the 'no plan produced' branch."""
    provider = _ScriptedProvider([
        ("text", "Just answering directly without a plan."),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    task = agent.run("what is Apex?")
    assert task.plan is None  # agent didn't submit one
    # Agent ran the planning phase before exit. We can't directly inspect
    # phase= but the conversation has the user request + assistant reply.
    assert any(
        m.get("role") == "user" and m.get("content") == "what is Apex?"
        for m in agent.conversation.as_messages()
    )


def test_execution_mode_skips_planning(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """Execution mode bypasses the planning phase. With an empty script
    the loop just hits end_turn and completes."""
    provider = _ScriptedProvider([
        ("text", "Done — running directly."),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    task = agent.run("answer me")
    # No plan produced and no awaiting-approval state.
    assert task.plan is None
    assert task.status.value == "complete"


def test_general_mode_skips_planning(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """General mode also skips the planning phase. Without any write
    tool calls, behaves identically to execution mode at this layer."""
    provider = _ScriptedProvider([
        ("text", "Read-only answer."),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
    )
    task = agent.run("just looking")
    assert task.plan is None
    assert task.status.value == "complete"


# ---------------------------------------------------------------------------
# Write gating across modes
# ---------------------------------------------------------------------------

def test_plan_mode_blocks_write_during_planning(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """Regression — plan mode's existing two-stage gate must still fire
    when the agent calls a write tool during the planning phase."""
    provider = _ScriptedProvider([
        ("tool", "file_write", {"file_path": "x.cls", "content": "y"}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    agent.run("do something")
    # Look for the blocked tool_result.
    msgs = agent.conversation.as_messages()
    found = any(
        isinstance(b, dict) and b.get("is_error") and "planning" in b.get(
            "content", "")
        for m in msgs if isinstance(m.get("content"), list)
        for b in m["content"]
    )
    assert found, "expected a 'cannot execute during planning' block"


def test_execution_mode_allows_writes_without_plan(
    org: OrgConnection, wm: WorkingMemoryStore, workspace: Path,
) -> None:
    """In execution mode, file_write should execute on the first call —
    no planning phase, no approval gate."""
    provider = _ScriptedProvider([
        ("tool", "file_write", {
            "file_path": "free.cls",
            "content": "public class Free {}",
        }),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("create the file")
    # The file should actually exist on disk.
    target = workspace / "ws" / "free.cls"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "public class Free {}"


def test_general_mode_blocks_write_when_user_says_no(
    org: OrgConnection, wm: WorkingMemoryStore, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User declines the inline approval → write is blocked, file
    not written."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *args, **kwargs: "no",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    provider = _ScriptedProvider([
        ("tool", "file_write", {
            "file_path": "denied.cls",
            "content": "should not exist",
        }),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
    )
    agent.run("write something")
    target = workspace / "ws" / "denied.cls"
    assert not target.exists()


def test_general_mode_executes_write_when_user_says_yes(
    org: OrgConnection, wm: WorkingMemoryStore, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User approves inline → write proceeds normally."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *args, **kwargs: "yes",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    provider = _ScriptedProvider([
        ("tool", "file_write", {
            "file_path": "yes.cls",
            "content": "approved",
        }),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
    )
    agent.run("write yes")
    target = workspace / "ws" / "yes.cls"
    assert target.exists()


def test_general_mode_always_caches_tool_for_session(
    org: OrgConnection, wm: WorkingMemoryStore, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selecting 'always' adds the tool to the allowlist — a second call
    to the same tool in the same session does NOT re-prompt."""
    prompt_calls: list[str] = []
    def fake_prompt(*args: Any, **kwargs: Any) -> str:
        prompt_calls.append("called")
        return "always"
    monkeypatch.setattr("sf_dev_agent.agent.Prompt.ask", fake_prompt)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    shared_allowlist: set[str] = set()
    # First task — user picks 'always' on the prompt.
    provider1 = _ScriptedProvider([
        ("tool", "file_write", {"file_path": "a.cls", "content": "1"}),
    ])
    agent1 = AgentLoop(
        org=org, provider=provider1, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
        write_allowlist=shared_allowlist,
    )
    agent1.run("write a")
    assert "file_write" in shared_allowlist
    assert len(prompt_calls) == 1

    # Second task with same shared allowlist — should NOT prompt again.
    provider2 = _ScriptedProvider([
        ("tool", "file_write", {"file_path": "b.cls", "content": "2"}),
    ])
    agent2 = AgentLoop(
        org=org, provider=provider2, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
        write_allowlist=shared_allowlist,
    )
    agent2.run("write b")
    assert len(prompt_calls) == 1, "second call should have been auto-approved"
    assert (workspace / "ws" / "b.cls").exists()


def test_general_mode_non_tty_auto_denies(
    org: OrgConnection, wm: WorkingMemoryStore, workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-TTY input (CI, piped) must auto-deny — never silently write."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    # Prompt.ask should NEVER be called in this case.
    def trap(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("Prompt.ask was called on non-TTY input")
    monkeypatch.setattr("sf_dev_agent.agent.Prompt.ask", trap)

    provider = _ScriptedProvider([
        ("tool", "file_write", {"file_path": "ci.cls", "content": "x"}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
    )
    agent.run("write under CI")
    assert not (workspace / "ws" / "ci.cls").exists()


def test_general_mode_cancel_aborts_loop(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User selects 'cancel' → KeyboardInterrupt bubbles to the existing
    handler; loop exits, task transitions appropriately."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *args, **kwargs: "cancel",
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    provider = _ScriptedProvider([
        ("tool", "file_write", {"file_path": "cancel.cls", "content": "z"}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.GENERAL, streaming=True,
    )
    task = agent.run("write z")
    # Cancel routes through the interrupt handler which transitions to FAILED.
    assert task.status.value in ("failed", "complete")
