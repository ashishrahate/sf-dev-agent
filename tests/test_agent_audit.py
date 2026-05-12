"""Integration tests for the agent loop's audit hook (Item 2).

Run a scripted provider through a real `AgentLoop.run()` and assert the
`llm_invocations` table fills up with the expected per-turn rows. Keeps
mocking to the LLM boundary so the loop's tool-execution + audit-write
glue is exercised end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from sf_dev_agent.agent import AgentLoop
from sf_dev_agent.audit import LLMAuditStore
from sf_dev_agent.memory import WorkingMemoryStore
from sf_dev_agent.models.schemas import AgentMode, OrgConnection
from sf_dev_agent.providers.base import (
    LLMProvider,
    LLMResponse,
    StreamChunk,
    StreamChunkKind,
    TokenUsage,
    consume_stream,
)


@pytest.fixture
def org() -> OrgConnection:
    return OrgConnection(
        tenant_id="t1", org_alias="OrgA",
        org_type="developer",
        instance_url="https://example.salesforce.com",
    )


@pytest.fixture(autouse=True)
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect default_db_path so the agent's audit store writes into a
    test-scoped SQLite. Also sets AGENT_WORKSPACE for any file-write tools
    that might land during a scripted run."""
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "sf_dev_agent.context.default_db_path", lambda: tmp_path / "wm.db",
    )
    return tmp_path


class _UsageScriptedProvider(LLMProvider):
    """Scripted streaming provider that emits known usage on every call.

    Each script item is one of:
        ("text", text, usage)
        ("tool", name, input, usage)
    """

    def __init__(self, script: list[tuple]) -> None:
        self.script = list(script)
        self.calls = 0

    @property
    def model_name(self) -> str:
        return "scripted-model-1"

    def chat(self, **kwargs: Any) -> LLMResponse:
        return consume_stream(self.chat_stream(**kwargs))

    def chat_stream(self, **kwargs: Any) -> Iterator[StreamChunk]:
        self.calls += 1
        if not self.script:
            yield StreamChunk(
                kind=StreamChunkKind.STOP, stop_reason="end_turn",
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            )
            return
        item = self.script.pop(0)
        if item[0] == "text":
            _, text, usage = item
            yield StreamChunk(kind=StreamChunkKind.TEXT_DELTA, text=text)
            yield StreamChunk(
                kind=StreamChunkKind.STOP, stop_reason="end_turn",
                usage=usage,
            )
        elif item[0] == "tool":
            _, name, inp, usage = item
            tool_id = f"tu_{self.calls}"
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_START,
                tool_id=tool_id, tool_name=name,
            )
            yield StreamChunk(
                kind=StreamChunkKind.TOOL_USE_END,
                tool_id=tool_id, tool_input=inp,
            )
            yield StreamChunk(
                kind=StreamChunkKind.STOP, stop_reason="tool_use",
                usage=usage,
            )


# ---------------------------------------------------------------------------
# Audit row population
# ---------------------------------------------------------------------------

def test_audit_writes_one_row_per_llm_call(
    org: OrgConnection, workspace: Path,
) -> None:
    """Two-iteration agent run: turn 0 emits a tool, turn 1 finalizes.
    Expect two rows in llm_invocations."""
    provider = _UsageScriptedProvider([
        ("tool", "code_search", {"query": "foo"},
         TokenUsage(input_tokens=200, output_tokens=15)),
        ("text", "all done", TokenUsage(input_tokens=300, output_tokens=25)),
    ])
    wm = WorkingMemoryStore(workspace / "wm.db")
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION,
    )
    task = agent.run("do a thing")
    wm.close()

    store = LLMAuditStore(workspace / "wm.db")
    try:
        rows = store.list_for_task(task.task_id)
    finally:
        store.close()

    assert len(rows) == 2
    # Turn 0: triggered_by_tool is None (first turn); emitted code_search.
    assert rows[0].turn_idx == 0
    assert rows[0].triggered_by_tool is None
    assert rows[0].emitted_tools == ["code_search"]
    assert rows[0].usage.input_tokens == 200
    assert rows[0].usage.output_tokens == 15
    assert rows[0].mode == "execution"
    assert rows[0].duration_ms >= 0
    # Turn 1: triggered by code_search; no tools emitted; end_turn.
    assert rows[1].turn_idx == 1
    assert rows[1].triggered_by_tool == "code_search"
    assert rows[1].emitted_tools == []
    assert rows[1].usage.input_tokens == 300
    assert rows[1].stop_reason == "end_turn"


def test_audit_attribution_chain_persists_across_turns(
    org: OrgConnection, workspace: Path,
) -> None:
    """Three iterations: tool A, tool B, then end. Each row's
    triggered_by_tool should point at the previous turn's tool."""
    provider = _UsageScriptedProvider([
        ("tool", "code_search", {"q": "1"},
         TokenUsage(input_tokens=100, output_tokens=10)),
        ("tool", "knowledge_search", {"q": "2"},
         TokenUsage(input_tokens=400, output_tokens=15)),
        ("text", "done", TokenUsage(input_tokens=600, output_tokens=20)),
    ])
    wm = WorkingMemoryStore(workspace / "wm.db")
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION,
    )
    task = agent.run("two tool chain")
    wm.close()

    store = LLMAuditStore(workspace / "wm.db")
    try:
        rows = store.list_for_task(task.task_id)
    finally:
        store.close()

    assert [r.triggered_by_tool for r in rows] == [
        None, "code_search", "knowledge_search",
    ]
    assert [r.emitted_tools for r in rows] == [
        ["code_search"], ["knowledge_search"], [],
    ]


def test_audit_records_provider_and_model(
    org: OrgConnection, workspace: Path,
) -> None:
    """Provider class name + model_name land in the row for `--by model`."""
    provider = _UsageScriptedProvider([
        ("text", "hi", TokenUsage(input_tokens=50, output_tokens=5)),
    ])
    wm = WorkingMemoryStore(workspace / "wm.db")
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION,
    )
    task = agent.run("just text")
    wm.close()

    store = LLMAuditStore(workspace / "wm.db")
    try:
        rows = store.list_for_task(task.task_id)
    finally:
        store.close()

    assert rows[0].provider == "_UsageScriptedProvider"
    assert rows[0].model == "scripted-model-1"


def test_audit_turn_idx_resets_per_task(
    org: OrgConnection, workspace: Path,
) -> None:
    """Reusing the same AgentLoop for a second task resets `_turn_idx` to 0
    so the new task's rows don't claim post-zero turn indexes."""
    provider = _UsageScriptedProvider([
        ("text", "first", TokenUsage(input_tokens=10, output_tokens=2)),
        ("text", "second", TokenUsage(input_tokens=20, output_tokens=3)),
    ])
    wm = WorkingMemoryStore(workspace / "wm.db")
    agent = AgentLoop(
        org=org, provider=provider, mock_org=True,
        working_memory=wm, mode=AgentMode.EXECUTION,
    )
    task1 = agent.run("first ask")
    task2 = agent.run("second ask")
    wm.close()

    store = LLMAuditStore(workspace / "wm.db")
    try:
        rows1 = store.list_for_task(task1.task_id)
        rows2 = store.list_for_task(task2.task_id)
    finally:
        store.close()

    assert rows1[0].turn_idx == 0
    assert rows2[0].turn_idx == 0
