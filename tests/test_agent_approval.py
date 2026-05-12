"""Tests for Slice 2 of the PI-style input-routing refactor.

The approval gate moves out of the agent loop into:
  - `AgentLoop.approve_plan(bool)` — caller-driven accept/reject
  - `AgentLoop.modify_plan(str)` — caller-driven revision
  - `agent.drive_approval_loop(agent)` — module-level default UX driver

`_request_approval` is gone. `_run_approval_then_execution` yields in
AWAITING_APPROVAL instead of blocking on `Prompt.ask`. The safety-net
branch refuses to silent-complete when the user's text looks like a
stray approval token (the original bug).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.agent import (
    AgentLoop,
    _looks_like_stray_approval,
    drive_approval_loop,
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


# ---------------------------------------------------------------------------
# Fixtures (mirror test_agent_modes.py — kept local so the slice owns its surface)
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
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )
    return tmp_path


class _ScriptedProvider(LLMProvider):
    """Scripted streaming provider. See test_agent_modes.py for shape."""

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


_MINIMAL_PLAN = {
    "summary": "two-step plan",
    "steps": [
        {"step_number": 1, "action": "draft", "target": "AccountTrigger",
         "mode": "create", "risk": "low", "description": "draft"},
        {"step_number": 2, "action": "deploy", "target": "AccountTrigger",
         "mode": "create", "risk": "low", "description": "deploy"},
    ],
    "risk_assessment": "low",
    "risk_reasoning": "additive",
    "rollback_strategy": "git revert",
}


def _agent_at_awaiting_approval(
    org: OrgConnection, wm: WorkingMemoryStore,
    *, monkeypatch: pytest.MonkeyPatch,
) -> AgentLoop:
    """Drive an agent through planning into AWAITING_APPROVAL.

    Stubs `drive_approval_loop` so run()'s auto-drive doesn't kick in.
    Returns the agent with `current_task.status == AWAITING_APPROVAL`.
    """
    provider = _ScriptedProvider([
        ("tool", "submit_plan", _MINIMAL_PLAN),
    ])
    # Suppress the auto-drive baked into run() so the test can exercise
    # approve_plan / modify_plan directly.
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    agent.run("write me a trigger")
    return agent


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("yes", True),
    ("YES", True),
    ("y", True),
    ("no", True),
    ("n", True),
    ("modify", True),
    ("  yes  ", True),
    ("", False),
    ("yes please", False),
    ("yeah", False),
    ("describe the Account object", False),
])
def test_looks_like_stray_approval(text: str, expected: bool) -> None:
    assert _looks_like_stray_approval(text) is expected


# ---------------------------------------------------------------------------
# approve_plan
# ---------------------------------------------------------------------------

def test_approve_plan_yes_runs_execution(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approve_plan(True) starts Phase 2 and ends in COMPLETE."""
    agent = _agent_at_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    assert agent.current_task.status == TaskStatus.AWAITING_APPROVAL
    # Phase 2 has no scripted tool calls — the LLM emits end_turn and the
    # agent transitions to COMPLETE.
    final = agent.approve_plan(True)
    assert final.status == TaskStatus.COMPLETE
    assert agent.plan_approved is True


def test_approve_plan_no_marks_failed(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approve_plan(False) transitions to FAILED with rejection reason."""
    agent = _agent_at_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    final = agent.approve_plan(False)
    assert final.status == TaskStatus.FAILED
    # The persisted summary explains the rejection.
    row = wm.get_task(agent.current_task.task_id)
    assert row.status == "failed"
    assert "rejected" in (row.result_json or "")


def test_approve_plan_rejects_when_not_awaiting(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """approve_plan refuses if called outside AWAITING_APPROVAL — guards
    against a REPL that double-calls or a stale agent state."""
    agent = AgentLoop(
        org=org, provider=_ScriptedProvider([]), mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN,
    )
    with pytest.raises(RuntimeError, match="no current_task"):
        agent.approve_plan(True)


# ---------------------------------------------------------------------------
# modify_plan
# ---------------------------------------------------------------------------

def test_modify_plan_with_followup_yields_awaiting_again(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """modify_plan re-runs planning; if a new plan emerges, stay in
    AWAITING_APPROVAL so the caller can re-prompt."""
    # Two scripted plans: first one for run(), second one for modify_plan().
    # Each is followed by a text/end_turn so the planning loop exits between
    # them — without that the agent would consume both plans in run().
    revised_plan = {**_MINIMAL_PLAN, "summary": "revised plan"}
    provider = _ScriptedProvider([
        ("tool", "submit_plan", _MINIMAL_PLAN),
        ("text", "Plan submitted."),
        ("tool", "submit_plan", revised_plan),
        ("text", "Revised plan submitted."),
    ])
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    agent.run("write me a trigger")
    assert agent.current_task.status == TaskStatus.AWAITING_APPROVAL

    final = agent.modify_plan("change the deploy step")
    # Still awaiting approval — the new plan is presented and the caller
    # is expected to drive another round.
    assert final.status == TaskStatus.AWAITING_APPROVAL
    assert agent.current_task.plan.summary == "revised plan"


def test_modify_plan_without_followup_marks_failed(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """modify_plan that yields no new plan must mark the task FAILED
    rather than leaving it in a half-state. This is the fix for the
    'modify recursion returns False, task stays awaiting' bug."""
    # First call submits a plan; second call (modify) emits only text,
    # no submit_plan → no new plan registered.
    provider = _ScriptedProvider([
        ("tool", "submit_plan", _MINIMAL_PLAN),
        ("text", "I changed my mind, no plan."),
    ])
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    agent.run("write me a trigger")
    final = agent.modify_plan("scrap the plan entirely")
    assert final.status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# drive_approval_loop
# ---------------------------------------------------------------------------

def test_drive_approval_loop_yes_path(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default driver routes 'yes' → approve_plan(True)."""
    agent = _agent_at_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *a, **kw: "yes",
    )
    final = drive_approval_loop(agent)
    assert final.status == TaskStatus.COMPLETE


def test_drive_approval_loop_no_path(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default driver routes 'no' → approve_plan(False)."""
    agent = _agent_at_awaiting_approval(org, wm, monkeypatch=monkeypatch)
    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *a, **kw: "no",
    )
    final = drive_approval_loop(agent)
    assert final.status == TaskStatus.FAILED


def test_drive_approval_loop_eof_leaves_task_awaiting(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EOFError on Prompt.ask leaves the task in AWAITING_APPROVAL so the
    REPL's busy gate (Slice 1) can surface a /resume hint next dispatch.
    The original bug was that a hung approval silently transitioned to
    COMPLETE; with the safety preserved here + slice 1's gate, it stays
    visible."""
    agent = _agent_at_awaiting_approval(org, wm, monkeypatch=monkeypatch)

    def _raise_eof(*a, **kw):
        raise EOFError

    monkeypatch.setattr("sf_dev_agent.agent.Prompt.ask", _raise_eof)
    final = drive_approval_loop(agent)
    # Task is preserved, not silent-completed.
    assert final is agent.current_task
    assert agent.current_task.status == TaskStatus.AWAITING_APPROVAL
    assert agent.is_busy is True


# ---------------------------------------------------------------------------
# Safety net — the original bug
# ---------------------------------------------------------------------------

def test_safety_net_fires_on_bare_yes_with_no_plan(
    org: OrgConnection, wm: WorkingMemoryStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the user types bare 'yes' as a new task (e.g. because a prior
    approval prompt got swallowed and the REPL routed it as fresh
    input), the no-plan branch refuses to silent-complete and marks
    the bogus task FAILED with a hint. This is the original bug fix."""
    provider = _ScriptedProvider([
        ("text", "yes what?"),
    ])
    # Disable auto-drive so we can observe the terminal state directly.
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    task = agent.run("yes")
    assert task.status == TaskStatus.FAILED
    out = capsys.readouterr().out
    assert "no active task" in out.lower()
    assert "/resume" in out


def test_no_plan_normal_question_still_completes(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal question that the agent answers directly (no plan) still
    transitions to COMPLETE — safety net only fires on bare approval
    tokens, not regular Q&A."""
    provider = _ScriptedProvider([
        ("text", "Apex is Salesforce's strongly-typed OO language."),
    ])
    monkeypatch.setattr(
        "sf_dev_agent.agent.drive_approval_loop", lambda agent: agent.current_task,
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    task = agent.run("what is Apex?")
    assert task.status == TaskStatus.COMPLETE


# ---------------------------------------------------------------------------
# run() integration — auto-drive preserved for one-shot CLI
# ---------------------------------------------------------------------------

def test_run_auto_drives_approval_via_default_driver(
    org: OrgConnection, wm: WorkingMemoryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: run() yields in AWAITING_APPROVAL, the default driver
    kicks in, monkeypatched 'yes' approves, and the task completes —
    same UX as before Slice 2 for one-shot CLI / resume_cli callers."""
    monkeypatch.setattr(
        "sf_dev_agent.agent.Prompt.ask",
        lambda *a, **kw: "yes",
    )
    provider = _ScriptedProvider([
        ("tool", "submit_plan", _MINIMAL_PLAN),
    ])
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.PLAN, streaming=True,
    )
    task = agent.run("create AccountTrigger")
    assert task.status == TaskStatus.COMPLETE
    assert agent.plan_approved is True
