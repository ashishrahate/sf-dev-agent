"""Tests for Slice 4 — `request_user_input` tool.

The LLM calls `request_user_input(question, choices?)` to pause
mid-execution for a clarifying answer. The handler persists the
question, transitions the task to AWAITING_USER_INPUT, signals the
loop to break, and the caller (REPL or default driver) prompts the
user. The answer feeds back via `agent.provide_user_input(answer)`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.agent import (
    AgentLoop,
    drive_user_input_loop,
)
from sf_dev_agent.memory import WorkingMemoryStore
from sf_dev_agent.models.schemas import AgentMode, OrgConnection, TaskStatus
from sf_dev_agent.providers.base import (
    LLMProvider,
    LLMResponse,
    StreamChunk,
    StreamChunkKind,
    consume_stream,
)


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
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )
    return tmp_path


class _ScriptedProvider(LLMProvider):
    def __init__(self, script: list[tuple]) -> None:
        self.script = list(script)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "scripted"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(self.chat_stream(**kwargs))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
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
# Tool registration
# ---------------------------------------------------------------------------

def test_request_user_input_registered(org: OrgConnection) -> None:
    """The tool definition is exposed to the LLM in all modes."""
    from sf_dev_agent.tools.registry import ToolRegistry

    reg = ToolRegistry(org=org, mock_org=True)
    defs = {d["name"]: d for d in reg.get_tool_definitions()}
    assert "request_user_input" in defs
    schema = defs["request_user_input"]["parameters"]["properties"]
    assert "question" in schema
    assert "choices" in schema


def test_request_user_input_present_in_all_modes(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """request_user_input is universal — every mode needs it (submit_plan
    is the only tool we filter; request_user_input stays)."""
    for mode in (AgentMode.PLAN, AgentMode.EXECUTION, AgentMode.GENERAL):
        agent = AgentLoop(
            org=org, provider=_ScriptedProvider([]), mock_org=True,
            working_memory=wm, mode=mode,
        )
        names = {d["name"] for d in agent._mode_filtered_tool_definitions()}
        assert "request_user_input" in names


# ---------------------------------------------------------------------------
# Handler + loop-break
# ---------------------------------------------------------------------------

def test_handler_sets_pending_question_and_status(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """request_user_input tool call sets _pending_question, transitions
    task to AWAITING_USER_INPUT, and breaks the loop."""
    # Disable the auto-drive helpers so the test observes the paused state.
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_user_input_loop", lambda agent: agent.current_task,
    )
    provider = _ScriptedProvider([
        ("tool", "request_user_input", {
            "question": "Which org should I deploy to?",
            "choices": ["sandbox", "production"],
        }),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    task = agent.run("deploy the new trigger")

    assert task.status == TaskStatus.AWAITING_USER_INPUT
    assert agent._pending_question == "Which org should I deploy to?"
    assert agent._pending_question_choices == ["sandbox", "production"]
    # Persisted to working memory so a crash-and-resume can read it back.
    row = wm.get_task(task.task_id)
    assert row.pending_question == "Which org should I deploy to?"
    assert row.status == "awaiting_user_input"


def test_handler_rejects_empty_question(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty/missing question returns is_error so the LLM can self-correct."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_user_input_loop", lambda agent: agent.current_task,
    )
    # Empty question, then a text/end_turn so the loop terminates after the
    # error tool_result rather than hanging.
    provider = _ScriptedProvider([
        ("tool", "request_user_input", {"question": ""}),
        ("text", "I'll try again later."),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("do something")
    # No pending_question landed.
    assert agent._pending_question is None
    # Task completed normally (no AWAITING_USER_INPUT).
    assert agent.current_task.status == TaskStatus.COMPLETE


# ---------------------------------------------------------------------------
# provide_user_input
# ---------------------------------------------------------------------------

def test_provide_user_input_resumes_and_completes(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the agent pauses, provide_user_input feeds the answer back,
    re-enters _agent_loop, and the task completes."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_user_input_loop", lambda agent: agent.current_task,
    )
    provider = _ScriptedProvider([
        ("tool", "request_user_input", {"question": "Pick one?"}),
        # After provide_user_input resumes the loop, this is consumed.
        ("text", "Got it, done."),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("kick off")
    assert agent.current_task.status == TaskStatus.AWAITING_USER_INPUT

    final = agent.provide_user_input("sandbox")
    assert final.status == TaskStatus.COMPLETE
    # The answer landed in the conversation as a user message.
    msgs = agent.conversation.as_messages()
    answers = [
        str(m.get("content", "")) for m in msgs
        if m.get("role") == "user"
    ]
    assert any(c == "sandbox" for c in answers)
    # pending_question cleared from both memory + DB.
    assert agent._pending_question is None
    row = wm.get_task(agent.current_task.task_id)
    assert row.pending_question is None


def test_provide_user_input_rejects_wrong_status(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION,
    )
    with pytest.raises(RuntimeError, match="no current_task"):
        agent.provide_user_input("anything")


# ---------------------------------------------------------------------------
# drive_user_input_loop
# ---------------------------------------------------------------------------

def test_drive_user_input_loop_prompts_and_resumes(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default driver prompts via Prompt.ask, feeds the answer through
    provide_user_input, and the run completes."""
    # Bind the real driver BEFORE the auto-drive monkeypatch so the test
    # call invokes the actual loop, not the stub.
    real_drive = drive_user_input_loop

    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_user_input_loop", lambda agent: agent.current_task,
    )
    provider = _ScriptedProvider([
        ("tool", "request_user_input", {"question": "Which one?"}),
        ("text", "Done."),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("hi")
    assert agent.current_task.status == TaskStatus.AWAITING_USER_INPUT

    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *a, **kw: "pick A",
    )
    final = real_drive(agent)
    assert final.status == TaskStatus.COMPLETE


def test_drive_user_input_loop_eof_leaves_awaiting(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOF/Interrupt on the prompt leaves the task in AWAITING_USER_INPUT
    so slice 1's busy gate surfaces /resume on the next dispatch."""
    real_drive = drive_user_input_loop

    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_user_input_loop", lambda agent: agent.current_task,
    )
    provider = _ScriptedProvider([
        ("tool", "request_user_input", {"question": "Continue?"}),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.run("hi")
    assert agent.current_task.status == TaskStatus.AWAITING_USER_INPUT

    def _raise_eof(*a, **kw):
        raise EOFError

    monkeypatch.setattr("sf_dev_agent.agent.Prompt.ask", _raise_eof)
    final = real_drive(agent)
    assert final is agent.current_task
    assert agent.current_task.status == TaskStatus.AWAITING_USER_INPUT
    assert agent.is_busy is True


# ---------------------------------------------------------------------------
# End-to-end via run() auto-drive
# ---------------------------------------------------------------------------

def test_run_auto_drives_user_input_to_completion(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: agent calls request_user_input, run()'s auto-drive
    prompts the user, the answer flows back, task completes — all in
    one run() call with no caller intervention."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *a, **kw: "yes do it",
    )
    provider = _ScriptedProvider([
        ("tool", "request_user_input", {"question": "Proceed?"}),
        ("text", "Done."),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    task = agent.run("start")
    assert task.status == TaskStatus.COMPLETE
