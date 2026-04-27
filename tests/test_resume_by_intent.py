"""Tests for resume-by-LLM-intent (Phase C.4).

Three tools that the agent can call when the user asks to resume work:
  - list_resumable_tasks: read-only browse of working memory
  - get_task_summary:     read-only transcript head for one task
  - request_resume:       intercepted; signals back to the REPL

The first two go through the registry like any other read-only tool.
The third is intercepted in `_execute_tool` and stamps
`agent.resume_requested = task_id`. The REPL reads that flag after
`run()` returns and dispatches `AgentLoop.resume(...)`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.memory import MemoryScope, WorkingMemoryStore
from sf_dev_agent.models.schemas import OrgConnection
from sf_dev_agent.providers.base import (
    LLMProvider,
    LLMResponse,
    StreamChunk,
    StreamChunkKind,
    consume_stream,
)
from sf_dev_agent.tools.registry import ToolRegistry


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


def _seed_task(
    wm: WorkingMemoryStore,
    *,
    task_id: str,
    scope: MemoryScope,
    user_request: str,
    status: str = "planning",
    messages: list[tuple[str, Any]] | None = None,
    plan_json: str | None = None,
) -> None:
    wm.create_task(task_id=task_id, scope=scope, user_request=user_request, status=status)
    if plan_json:
        wm.set_plan(task_id, plan_json)
    for role, content in messages or []:
        wm.append_message(task_id, role, content)


# ---------------------------------------------------------------------------
# list_resumable_tasks
# ---------------------------------------------------------------------------

def test_list_resumable_tasks_returns_only_in_flight_by_default(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
    _seed_task(wm, task_id="t_open", scope=scope, user_request="open work", status="planning")
    _seed_task(wm, task_id="t_done", scope=scope, user_request="finished", status="complete")

    registry = ToolRegistry(org=org, mock_org=True, working_memory=wm)
    out = registry.execute("list_resumable_tasks", {})

    ids = [t["task_id"] for t in out["tasks"]]
    assert "t_open" in ids
    assert "t_done" not in ids
    assert out["count"] == 1


def test_list_resumable_tasks_with_include_terminal_includes_complete(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
    _seed_task(wm, task_id="t_open", scope=scope, user_request="open", status="planning")
    _seed_task(wm, task_id="t_done", scope=scope, user_request="done", status="complete")

    registry = ToolRegistry(org=org, mock_org=True, working_memory=wm)
    out = registry.execute("list_resumable_tasks", {"include_terminal": True})

    ids = {t["task_id"] for t in out["tasks"]}
    assert {"t_open", "t_done"} <= ids


def test_list_resumable_tasks_returns_error_when_no_working_memory(
    org: OrgConnection,
) -> None:
    registry = ToolRegistry(org=org, mock_org=True, working_memory=None)
    out = registry.execute("list_resumable_tasks", {})
    assert "error" in out
    assert out["tasks"] == []


# ---------------------------------------------------------------------------
# get_task_summary
# ---------------------------------------------------------------------------

def test_get_task_summary_returns_message_head(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
    _seed_task(
        wm, task_id="tsum", scope=scope, user_request="describe an account",
        messages=[
            ("user", "describe an account"),
            ("assistant", [
                {"type": "text", "text": "I'll look at the schema."},
                {"type": "tool_use", "id": "x", "name": "code_search", "input": {"q": "Account"}},
            ]),
            ("user", [{"type": "tool_result", "tool_use_id": "x", "content": "..."}]),
        ],
    )

    registry = ToolRegistry(org=org, mock_org=True, working_memory=wm)
    out = registry.execute("get_task_summary", {"task_id": "tsum"})

    assert out["task_id"] == "tsum"
    assert out["message_count"] == 3
    assert len(out["head"]) == 3
    assert out["head"][0]["role"] == "user"
    # Assistant block summary should mention the tool by name.
    assistant_snippet = out["head"][1]["content"]
    assert "code_search" in assistant_snippet


def test_get_task_summary_unknown_task(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    registry = ToolRegistry(org=org, mock_org=True, working_memory=wm)
    out = registry.execute("get_task_summary", {"task_id": "no-such-task"})
    assert "error" in out
    assert "not found" in out["error"]


def test_get_task_summary_rejects_cross_tenant(
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    other_scope = MemoryScope(tenant_id="other-tenant", org_alias="OrgX")
    _seed_task(wm, task_id="tcross", scope=other_scope, user_request="not yours")

    registry = ToolRegistry(org=org, mock_org=True, working_memory=wm)
    out = registry.execute("get_task_summary", {"task_id": "tcross"})
    assert "error" in out
    assert "different tenant" in out["error"]


# ---------------------------------------------------------------------------
# request_resume — intercepted in _execute_tool, sets agent.resume_requested
# ---------------------------------------------------------------------------

class _ResumeIntentProvider(LLMProvider):
    """Streaming provider that emits one tool_use chunk on first call,
    then end_turn. Used to simulate the LLM calling request_resume."""

    def __init__(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "resume-intent"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(self.chat_stream(**kwargs))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        self.calls += 1
        if self.calls == 1:
            yield StreamChunk(
                kind=StreamChunkKind.TEXT_DELTA,
                text="Picking up your last task.",
            )
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_START,
                tool_id="rrid", tool_name=self.tool_name,
            )
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_END,
                tool_id="rrid", tool_input=self.tool_input,
            )
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="tool_use")
        else:
            yield StreamChunk(kind=StreamChunkKind.STOP, stop_reason="end_turn")


def test_request_resume_sets_agent_resume_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """When the LLM calls request_resume(task_id), the agent stamps
    `resume_requested` on itself and ends the loop without executing
    the registry executor for that tool."""
    from sf_dev_agent.agent import AgentLoop

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    # Seed a target task for the resume to point at.
    scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
    _seed_task(wm, task_id="t_target", scope=scope, user_request="original work")

    provider = _ResumeIntentProvider(
        tool_name="request_resume",
        tool_input={"task_id": "t_target", "rationale": "user asked"},
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, streaming=True,
    )
    agent.run("resume what I was doing")

    assert agent.resume_requested == "t_target"

    # The tool_result block should reflect the synthetic confirmation,
    # not an error.
    msgs = agent.conversation.as_messages()
    tool_result_blocks = []
    for m in msgs:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tool_result_blocks.append(b)
    assert tool_result_blocks, "expected a tool_result block from request_resume"
    payload = json.loads(tool_result_blocks[-1]["content"])
    assert payload.get("resume_signaled") is True
    assert payload.get("task_id") == "t_target"


def test_request_resume_rejects_unknown_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """A request_resume with a non-existent task_id should NOT signal
    the REPL; it should return an is_error tool_result instead."""
    from sf_dev_agent.agent import AgentLoop

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    provider = _ResumeIntentProvider(
        tool_name="request_resume",
        tool_input={"task_id": "does-not-exist"},
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, streaming=True,
    )
    agent.run("try to resume nothing")

    assert agent.resume_requested is None


def test_request_resume_rejects_cross_tenant_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    from sf_dev_agent.agent import AgentLoop

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    other_scope = MemoryScope(tenant_id="other-tenant", org_alias="OrgX")
    _seed_task(wm, task_id="t_other", scope=other_scope, user_request="not yours")

    provider = _ResumeIntentProvider(
        tool_name="request_resume",
        tool_input={"task_id": "t_other"},
    )
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, streaming=True,
    )
    agent.run("attempt cross-tenant resume")

    assert agent.resume_requested is None


# ---------------------------------------------------------------------------
# REPL hand-off — _dispatch_agent calls AgentLoop.resume() when signaled
# ---------------------------------------------------------------------------

def test_repl_dispatches_agentloop_resume_on_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    org: OrgConnection, wm: WorkingMemoryStore,
) -> None:
    """After agent.run() returns with resume_requested set, the REPL
    should invoke AgentLoop.resume() once with that task_id."""
    from sf_dev_agent.repl import ReplSession

    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )

    scope = MemoryScope(tenant_id=org.tenant_id, org_alias=org.org_alias)
    _seed_task(wm, task_id="t_target", scope=scope, user_request="real work")

    # First run: the LLM signals a resume. Subsequent calls (inside
    # AgentLoop.resume) get end_turn so the resumed task completes
    # without any tool calls.
    provider = _ResumeIntentProvider(
        tool_name="request_resume",
        tool_input={"task_id": "t_target"},
    )

    resumes_called: list[str] = []

    real_resume = __import__(
        "sf_dev_agent.agent", fromlist=["AgentLoop"]
    ).AgentLoop.resume

    def tracked_resume(*args: Any, task_id: str, **kwargs: Any) -> Any:
        resumes_called.append(task_id)
        return real_resume(*args, task_id=task_id, **kwargs)

    monkeypatch.setattr(
        "sf_dev_agent.agent.AgentLoop.resume",
        classmethod(lambda cls, **kw: (
            resumes_called.append(kw["task_id"]),
            cls(
                org=kw["org"], provider=kw["provider"],
                working_memory=kw["working_memory"],
                mock_org=kw.get("mock_org", False),
            ).current_task,
        )[1]),
    )

    session = ReplSession(
        org=org, provider=provider,
        working_memory=wm, mock_org=False,
    )
    directive = session._dispatch_agent("resume my work")

    from sf_dev_agent.repl_commands import ReplDirective
    assert directive == ReplDirective.CONTINUE
    assert resumes_called == ["t_target"]
