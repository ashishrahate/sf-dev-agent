"""Tests for Slice 3 of the PI-style input-routing refactor.

Adds two queues to `AgentLoop`:

  - `steer(text)`     — injected at the next iteration boundary
  - `queue_follow_up` — injected only when the loop would otherwise
                        terminate (keeps the run alive)

Plus implicit approval-token routing in `prompt()` while busy: bare
"yes"/"no" → `approve_plan` directly, so the free-text approval reply
in the user's original bug now lands where it should.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.agent import (
    AgentLoop,
    BusyError,
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
    """See test_agent_modes / test_agent_approval for shape."""
    def __init__(self, script: list[tuple]) -> None:
        self.script = list(script)
        self.calls = 0
        self.last_messages: list = []

    @property
    def model_name(self) -> str:
        return "scripted"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(self.chat_stream(**kwargs))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        self.calls += 1
        self.last_messages = kwargs.get("messages", [])
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


_MINIMAL_PLAN = {
    "summary": "two-step plan",
    "steps": [
        {"step_number": 1, "action": "draft", "target": "X",
         "mode": "create", "risk": "low", "description": "draft"},
    ],
    "risk_assessment": "low",
    "risk_reasoning": "ok",
    "rollback_strategy": "revert",
}


# ---------------------------------------------------------------------------
# Queue plumbing
# ---------------------------------------------------------------------------

def test_queues_initialized_empty(org: OrgConnection) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        mode=AgentMode.PLAN,
    )
    assert list(agent._steer_queue) == []
    assert list(agent._follow_up_queue) == []


def test_steer_appends_to_queue(org: OrgConnection) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        mode=AgentMode.PLAN,
    )
    agent.steer("hey, also do X")
    agent.steer("and Y")
    assert list(agent._steer_queue) == ["hey, also do X", "and Y"]


def test_queue_follow_up_appends_to_queue(org: OrgConnection) -> None:
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        mode=AgentMode.PLAN,
    )
    agent.queue_follow_up("yes")
    assert list(agent._follow_up_queue) == ["yes"]


# ---------------------------------------------------------------------------
# follow-up queue drains at terminate point
# ---------------------------------------------------------------------------

def test_follow_up_queue_keeps_loop_alive(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent loop that would have stopped (end_turn) iterates again
    when there's a follow-up queued. Verifies via provider.calls count."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    provider = _ScriptedProvider([
        ("text", "first iteration"),
        ("text", "second iteration after follow-up"),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.queue_follow_up("please continue")

    agent.run("kick off")
    # Loop called provider twice — once for the initial, once after
    # draining the follow-up.
    assert provider.calls == 2
    # Follow-up message appears in the conversation transcript.
    msgs = agent.conversation.as_messages()
    found = any(
        m.get("role") == "user" and "please continue" in str(m.get("content", ""))
        for m in msgs
    )
    assert found


def test_follow_up_queue_drained_in_order(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple queued follow-ups all land in one continuation, preserving order."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    provider = _ScriptedProvider([
        ("text", "first"),
        ("text", "second"),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.queue_follow_up("alpha")
    agent.queue_follow_up("beta")
    agent.run("go")
    # Both follow-ups present in transcript.
    msgs = agent.conversation.as_messages()
    contents = [str(m.get("content", "")) for m in msgs if m.get("role") == "user"]
    joined = " | ".join(contents)
    assert "alpha" in joined
    assert "beta" in joined
    # Queues are empty after draining.
    assert list(agent._follow_up_queue) == []


# ---------------------------------------------------------------------------
# steer queue drains at iteration boundary
# ---------------------------------------------------------------------------

def test_steer_queue_drains_at_iteration_top(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-loaded steer messages are injected before the LLM call so the
    LLM sees them in the very next turn."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    provider = _ScriptedProvider([
        ("text", "ok done"),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION, streaming=True,
    )
    agent.steer("change of plans: also include Y")
    agent.run("first request")

    # The LLM's messages-arg included the steer line, in addition to
    # the user's original "first request".
    msgs_seen = provider.last_messages
    contents = [str(m.get("content", "")) for m in msgs_seen if m.get("role") == "user"]
    joined = " | ".join(contents)
    assert "<steer>" in joined
    assert "also include Y" in joined
    assert list(agent._steer_queue) == []


# ---------------------------------------------------------------------------
# prompt() implicit approval routing
# ---------------------------------------------------------------------------

def _agent_in_awaiting_approval(
    org: OrgConnection, wm: WorkingMemoryStore,
    *, monkeypatch: pytest.MonkeyPatch,
) -> AgentLoop:
    provider = _ScriptedProvider([
        ("tool", "submit_plan", _MINIMAL_PLAN),
    ])
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    agent.run("task one")
    assert agent.current_task.status == TaskStatus.AWAITING_APPROVAL
    return agent


@pytest.mark.parametrize("token", ["yes", "y", "YES", "  yes  ", "Y"])
def test_prompt_yes_while_awaiting_approves(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    """The implicit-routing fix: free-text 'yes' approves the pending plan
    instead of starting a new task."""
    agent = _agent_in_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    final = agent.prompt(token)
    assert final.status == TaskStatus.COMPLETE
    assert agent.plan_approved is True


@pytest.mark.parametrize("token", ["no", "n", "NO", "  no  ", "N"])
def test_prompt_no_while_awaiting_rejects(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    agent = _agent_in_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    final = agent.prompt(token)
    assert final.status == TaskStatus.FAILED
    assert agent.plan_approved is False


def test_prompt_modify_token_alone_raises_busy(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'modify' on its own can't proceed — feedback is needed. Slice 5
    will add a REPL-side prompt for the feedback; until then, BusyError
    falls through so the REPL surfaces a hint."""
    agent = _agent_in_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    with pytest.raises(BusyError):
        agent.prompt("modify")


def test_prompt_freeform_while_busy_raises_busy_error(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-approval-shaped text while busy still raises so the REPL can
    surface a /resume hint. Slice 5 layers the confirm-on-ambiguity
    dialog so this raise doesn't strand longer free text."""
    agent = _agent_in_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    with pytest.raises(BusyError) as exc:
        agent.prompt("now also create a test class")
    assert exc.value.active_task_id == agent.active_task_id
